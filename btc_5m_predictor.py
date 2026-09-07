#!/usr/bin/env python3
"""
btc_5m_predictor.py
===================

Automated trader for Binance Wallet Prediction Markets, restricted to the
BTC 5-minute Up/Down market.

STRATEGY
--------
Price a 5m up/down contract as a digital option off the market's own price
feed, and buy only when the venue quotes materially less than the model's
probability. The edge is the gap between price and true probability -- NOT
the win rate.

WHY NO MARTINGALE
-----------------
An outcome share costs p and pays 1, so break-even win rate IS p. Buying at
0.95 gives a 95% win rate with +5.3% wins and -100% losses; with full-stack
sizing the 1-in-20 loss arrives on a schedule (expected ruin ~20 rounds).
Sizing here is fractional Kelly, the correct formalisation of "stay in the
game."

ENDPOINTS AND SCHEMAS
---------------------
Paths and payload shapes below are taken from Binance's own auto-generated
OpenAPI connector, `@binance/w3w-prediction` (npm). That is the authoritative
source; several published doc pages 404 after their docs migration.

Notable consequences, each of which silently breaks naive implementations:
  * All amounts are wei, 18 decimals. 1 USDT == "1000000000000000000".
  * Trading is two-phase: get-quote returns a quoteId + averagePrice, then
    place-order-bundle executes that quoteId. MARKET orders are FOK.
  * The order response returns only orderId. The fill price is the quote's
    averagePrice.
  * The strike is variantData.startPrice, and the market resolves on ITS OWN
    price feed (variantData.priceFeedProvider / priceFeedSymbol), not on
    Binance spot. Modelling off Binance spot introduces basis risk, so the
    strike and settlement both come from the venue.
  * wallet/list returns addresses, not balances. Balances come from
    balance/payment-options.

Still unverified without a funded account: live behaviour of
place-order-bundle. Run --preflight first; it probes everything else.

REQUIREMENTS
------------
    pip install requests
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import hmac
import itertools
import json
import logging
import math
import os
import re
import signal
import sqlite3
import statistics
import sys
import threading
import time
import urllib.parse
import queue
from collections.abc import Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from enum import Enum

import requests

LOG = logging.getLogger("btc5m")

BASE = "https://api.binance.com"
# Target round length. A constant here would silently exclude every market if
# the venue ever lists a different cadence; the tolerance is a fraction of the
# target rather than a fixed number of seconds.
DEFAULT_ROUND_SECONDS = 300
WEI = Decimal(10) ** 18

# Tolerance for float comparisons on money and probabilities. One name so a
# later change cannot leave some comparisons stricter than others.
EPS = 1e-9

# Fallback only, for journal rows written before the fee column existed.
DEFAULT_FEE_BPS = 200

# Verified against @binance/w3w-prediction. Overridable in the config
# file under "endpoints"; run --write-config to generate one.
# (HTTP method, path). The verb travels WITH the path: keeping them apart is
# what produced "Request method 'GET' is not supported" on trade/get-quote.
# Methods verified against @binance/w3w-prediction 2.0.1.
DEFAULT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "category_list": ("GET", "/sapi/v1/w3w/wallet/prediction/category/list"),
    "market_list": ("GET", "/sapi/v1/w3w/wallet/prediction/market/list"),
    "market_detail": ("GET", "/sapi/v1/w3w/wallet/prediction/market/detail"),
    "order_book": ("GET", "/sapi/v1/w3w/wallet/prediction/order-book"),
    "last_trade_price": ("GET", "/sapi/v1/w3w/wallet/prediction/order-book/last-trade-price"),
    "wallet_list": ("GET", "/sapi/v1/w3w/wallet/prediction/wallet/list"),
    "balances": ("GET", "/sapi/v1/w3w/wallet/prediction/balance/payment-options"),
    "quota_status": ("GET", "/sapi/v1/w3w/wallet/prediction/quota/limit/status"),
    "get_quote": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/get-quote"),
    "place_order": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/place-order-bundle"),
    "positions": ("GET", "/sapi/v1/w3w/wallet/prediction/position/list"),
    "settled_history": ("GET", "/sapi/v1/w3w/wallet/prediction/position/settled-history"),
    "order_history": ("GET", "/sapi/v1/w3w/wallet/prediction/order/history"),
    "batch_redeem": ("POST", "/sapi/v1/w3w/wallet/prediction/batch-redeem"),
    "redeem_status": ("GET", "/sapi/v1/w3w/wallet/prediction/redeem/status"),
    "portfolio": ("GET", "/sapi/v1/w3w/wallet/prediction/pnl/portfolio"),
    # Transfers are handled inline by place-order's fundTransferAmount, so
    # the standalone transfer endpoints are deliberately not wired up.
}


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def to_wei(amount_usdt: float | Decimal) -> str:
    """
    USDT -> wei string, truncated (never rounded up past the balance).

    Decimal throughout: float arithmetic on 18 decimals loses precision and
    would produce off-by-a-few-wei amounts the venue may reject.
    """
    d = Decimal(str(amount_usdt))
    if d <= 0:
        raise ValueError("amount must be positive")
    return str(int((d * WEI).to_integral_value(rounding=ROUND_DOWN)))


def from_wei(amount_wei: str | int) -> Decimal:
    """wei -> USDT as Decimal."""
    return Decimal(str(amount_wei)) / WEI


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    live: bool = True

    # --- Edge --------------------------------------------------------------
    # Two thresholds, both of which must clear. The absolute floor stops us
    # trading noise; the ratio keeps the bar consistent across price levels.
    # An absolute 0.06 edge is a 7% margin at price 0.90 but a 60% margin at
    # 0.10 -- one number cannot serve both ends of the book.
    min_edge: float = 0.02
    min_edge_ratio: float = 0.30      # model_prob >= breakeven * 1.30
    # How far spot must sit from the strike, measured in standard deviations
    # of the REMAINING time. This is "big buffer AND late in the round"
    # expressed as one number: the same buffer is worth more with less time
    # left, and z captures both at once. 0 disables the gate.
    min_buffer_sigmas: float = 0.0
    # Minimum NET profit per unit staked on a win, after the venue's fee.
    #
    # A positive edge says a bet is worth making; it says nothing about
    # whether the payout is worth the risk of ruin attached to it. Buying at
    # 0.94 returns about 6% on a win, so one loss erases sixteen wins. The
    # arithmetic is unforgiving in a way the edge test cannot see, because
    # edge is measured in probability and this is measured in money.
    #
    # 0.25 means "a win must pay at least 25% of the stake", which caps the
    # fill price at (1-fee)/((1-fee)+0.25) -- about 0.797 at a 2% fee. The
    # cap is derived from the fee rather than written down, so a market with
    # a different fee gets a different ceiling automatically.
    #
    # 0.0 disables it and the entry band alone governs.
    min_win_return: float = 0.0
    # FALLBACK only. Each market publishes its own feeRateBps and that value
    # wins. Assuming 2% when the real rate is near zero silently demands about
    # a point of extra edge that does not exist, and suppresses valid trades.
    fee_bps: int = 200
    # Deliberately permissive defaults. Every profile sets these explicitly;
    # a convex-shaped default silently narrowed any custom config that did not.
    max_entry_price: float = 0.95
    min_entry_price: float = 0.05
    # Haircut on an indicative quote when the book cannot be read, as a
    # FRACTION of the price. A flat 0.03 is 60% of a 0.05 longshot but 3% of
    # a 0.95 near-certainty -- one absolute number cannot serve both ends.
    assumed_spread_pct: float = 0.05
    max_price_impact: float = 0.05      # reject quotes that move the book far

    # --- Timing ------------------------------------------------------------
    entry_window_start_s: int = 150
    entry_window_end_s: int = 25

    # --- Risk --------------------------------------------------------------
    kelly_fraction: float = 0.25
    max_stake_pct: float = 0.05
    # Small accounts: allow betting the venue minimum when Kelly sizes below
    # it, bounded by the 2x-full-Kelly limit inside kelly_stake.
    round_up_to_minimum: bool = True
    # Absolute ceiling for the small-account override, as a MULTIPLE of
    # max_stake_pct rather than an independent number. Expressed absolutely
    # it had to be kept >= max_stake_pct by every caller, and any config that
    # raised one without the other failed validation. As a multiple the
    # invariant holds by construction and cannot be violated.
    hard_stake_multiple: float = 2.5
    hard_stake_ceiling: float = 0.35
    # Scale-in: start small, then top up as the round moves in our favour.
    # This is NOT martingale -- martingale adds after LOSSES, chasing. This
    # adds only when the position is winning and the model's probability has
    # risen, and it tops up toward the Kelly stake for the CURRENT
    # probability rather than stacking independent bets. Total exposure to a
    # single round is therefore still governed by Kelly, not multiplied by it.
    # Add unclaimed winnings to the live bankroll?
    #
    # Off by default. The portfolio's totalCurrentValue already counts settled
    # positions, so adding the gross payout on top double-counts it -- and the
    # error is the GROSS payout (stake + profit), not the profit, so a 1.00
    # stake winning at 0.60 inflated the reported bankroll by 1.67 rather than
    # 0.65. Turn it on only if reconciliation shows the API genuinely excludes
    # unredeemed winnings.
    count_unredeemed_in_bankroll: bool = False
    # Confirm every live order actually FILLED before recording a position.
    #
    # PlaceOrderResponse carries only an orderId -- nothing about fills. With
    # timeInForce=FOK an order that cannot fill is KILLED, and the venue still
    # returns an id. Trusting that id books a position that never existed,
    # which then "settles" and reports a profit that was never made. The only
    # way to know is to ask the venue.
    confirm_fills: bool = True
    fill_confirm_attempts: int = 5
    fill_confirm_delay_s: float = 1.0
    # A fill this far below the amount requested is treated as a failure
    # rather than a partial position.
    min_fill_fraction: float = 0.90
    # Warn when the API balance moves by more than this fraction away from the
    # P&L we expected. Catches double-counting, unexpected fees and silent
    # partial fills.
    reconcile_tolerance: float = 0.10
    scale_in: bool = False
    scale_in_initial_pct: float = 0.4     # first tranche, as a share of target
    scale_in_min_topup: float = 1.0       # skip top-ups below the order min
    # Ceiling on the BLENDED fill price across all tranches of one round.
    #
    # This is the knob that controls "how many wins does it take to cover a
    # loss", because that number is 1/((1-p)/p) at the blended price p and
    # nothing else. Top-ups happen when the round has moved our way, which
    # means the price has RISEN -- so every top-up drags the blended price up
    # and the payout down. Left unbounded, a large top-up at 0.95 can turn a
    # 6-wins-per-loss position into a 15-wins-per-loss one.
    #
    # A top-up is trimmed to whatever keeps the blend under this cap, and
    # skipped if that leaves less than the minimum order.
    #
    # Meaningful only relative to a profile's own band, so every profile sets
    # it: 0.90 suits buffer (0.80-0.97) and is inert for convex (0.05-0.35),
    # where no fill could ever approach it. Validated against the band below.
    max_blended_price: float = 0.90
    # The connector documents ~1.5 USDT as an APPROXIMATE MARKET-order
    # minimum that "varies by market depth"; the account minimum is 1.00.
    # Small MARKET orders may be rejected on a thin book -- that surfaces as a
    # quote error, not a silent loss.
    min_stake_usdt: float = 1.0
    daily_loss_limit_pct: float = 0.20
    # A low-win-rate strategy produces long losing streaks by design, so a
    # raw streak counter is the wrong instrument -- at a 25% hit rate, four
    # losses in a row happens roughly every five trades. The real question is
    # whether results are significantly worse than the model predicted, which
    # calibration_z answers. The streak cap remains only as a crude backstop.
    max_consecutive_losses: int = 30
    calibration_min_samples: int = 30
    calibration_z_halt: float = -2.5
    max_rounds_per_day: int = 200
    max_consecutive_errors: int = 20

    # --- Venue -------------------------------------------------------------
    # chainId, collateral, fee and the venue's own slippage all come from the
    # market payload. Only the values below are genuinely our decisions.
    round_seconds: int = DEFAULT_ROUND_SECONDS
    max_slippage_bps: int = 300          # OUR risk cap; venue default is 1200
    min_liquidity: float = 0.0           # skip markets thinner than this
    account_type: str = "AUTO"           # AUTO | SPOT | FUNDING
    # AUTO derives it from where the balance actually sits. Hardcoding "MPC"
    # while paying from a SPOT/FUNDING account is self-contradictory: those
    # are CEX accounts, and the venue rejects the combination (-3026).
    funding_source: str = "AUTO"         # AUTO | MPC | CEX
    auto_fund_transfer: bool = True      # move collateral for CEX-funded orders
    open_statuses: tuple[str, ...] = ("REGISTERED", "OPEN", "ACTIVE")
    tradable_status: str = "OPEN"
    # Markets to trade. Each is treated as an independent instrument: its own
    # position slot, its own loss streak, its own calibration statistics and
    # its own volatility and tail estimates. What CANNOT be isolated is the
    # bankroll -- there is one account -- so concurrency is capped and sizing
    # runs against uncommitted funds, or N markets quietly stack to N times
    # the intended exposure.
    #
    # Empty (the default) means NO restriction: every 5-minute up/down market
    # the venue lists is discovered and traded automatically, capped only by
    # max_concurrent_positions. Set one or more tickers to trade only those.
    symbols: tuple[str, ...] = ()
    max_concurrent_positions: int = 2
    # Reserve, as a fraction of bankroll, kept free regardless of how many
    # markets look attractive at once.
    reserve_pct: float = 0.30

    # --- Model -------------------------------------------------------------
    # Fat tails, estimated from realised kurtosis at runtime. None => Gaussian.
    use_fat_tails: bool = True
    tail_df_floor: float = 2.5
    tail_df_ceiling: float = 30.0
    # Sigma only needs a short window; kurtosis needs a long one (its standard
    # error is ~sqrt(24/n), so 60 samples cannot distinguish fat tails from
    # noise at all). Fetch long, measure sigma on the recent tail of it.
    vol_lookback_min: int = 500
    sigma_window_min: int = 60
    # The floor exists only to avoid degenerate maths, so it must sit well
    # below any plausible real value. If it ever binds, the model is asserting
    # a volatility rather than measuring one -- and an overstated sigma
    # inflates exactly the tail probabilities the convex profile buys, by up
    # to 3.5x in observed cases. Trading is refused rather than distorted.
    vol_floor_annual: float = 0.03
    vol_ceiling_annual: float = 3.00
    halt_on_clamped_sigma: bool = True

    # --- Trend (inertia) ---------------------------------------------------
    # BTC often runs in one direction for several consecutive rounds. While
    # that lasts, a trade taken early -- before the buffer is large enough
    # for the venue to have repriced -- is both cheaper and more likely to
    # win. That is the whole opportunity, and it is also the whole risk: the
    # run can end on the very round you size up on, and a market oscillating
    # around the strike produces a sequence of one-round "runs" that are pure
    # noise. Every setting below exists to tell those two apart.
    trend_follow: bool = False
    # Minutes of 1m history the trend is measured over. Read from the same
    # klines the volatility estimate already fetches, so this costs nothing.
    trend_lookback_min: int = 30
    # THE PRIMARY GATE. Size of the move over the last round-length, in
    # standard deviations of one such block. This is what fires at the START
    # of a trend: a thrust happening right now scores high on its first
    # block, whereas a count of completed rounds cannot say anything until
    # the move is already old and the price already bad.
    trend_min_impulse: float = 1.2
    # Blocks in the same direction. The MINIMUM is 1 on purpose -- a strong
    # first thrust is a trend beginning, and demanding corroboration means
    # systematically entering late.
    trend_min_run: int = 1
    # The maximum is the brake. A run that has already gone this far is
    # nearer its end than its beginning, and this is the mechanical version
    # of "keep an eye on it as it fades": the boost switches itself off
    # rather than waiting for a loss to switch it off.
    trend_max_run: int = 5
    # Current block's size relative to the previous one. Below this the move
    # is giving back momentum block over block and is dying, however
    # impressive its history looks.
    trend_decay_floor: float = 0.55
    # Projected rounds of life left before the move decays under the impulse
    # floor. 1.0 means "must survive the round I am about to enter", which is
    # the only horizon an entry actually cares about. Raising it demands the
    # move outlast the round with margin.
    trend_min_rounds_left: float = 1.0
    # Net move over the run, in sigmas of the run. Secondary to impulse:
    # it confirms the move is real rather than announcing it.
    trend_min_z: float = 0.8
    # Net displacement divided by the total distance travelled, over the run.
    # A market swinging across the strike covers a lot of ground and ends up
    # nowhere, which scores near zero here and is exactly the case to sit out.
    trend_min_efficiency: float = 0.35
    # How much larger the stake may be while the trend is confirmed AND
    # points the same way as the trade. Still bounded by the hard stake cap
    # and by twice full Kelly, so the boost cannot reach a size that loses
    # money in the long run.
    trend_stake_multiple: float = 1.5
    # How much earlier entry is allowed while the trend is confirmed, in
    # seconds added to entry_window_start_s. Entering early is what makes the
    # price worth having; the buffer gate still has to clear, and because it
    # is measured in sigmas of the REMAINING time, clearing it early takes a
    # genuinely larger move rather than a more lenient test.
    trend_early_entry_s: int = 90

    # --- Straddle (buy both sides at round-open) ----------------------------
    # A different strategy entirely: no probability model, no picking a
    # side. The edge here is that the venue's own pricing sometimes has not
    # converged to a fair ~50/50 in the first seconds of a round, so buying
    # BOTH outcomes locks in a profit whichever way it resolves -- PROVIDED
    # the prices actually paid support that. Every live round is entered
    # (subject only to the worst-case check below): skipping a round on a
    # directional hunch is exactly the judgement this mode does not make.
    straddle: bool = False
    # Stake on EACH side, as a fraction of bankroll. Total committed to one
    # round is roughly double this number, not this number itself.
    straddle_stake_pct: float = 0.05
    # How long after a round opens a FIRST leg may still be started.
    #
    # This window buys RUNWAY, not cheapness. The two legs are bought at
    # different moments and only the pair is a straddle, so the scarce thing
    # is not the entry price -- it is the time left to find the other side
    # after the first is on the book. An opener at t=240 of a 300s round has
    # 60 seconds to be hedged in and usually is not; one at t=30 has four
    # minutes. Open inside the first minute, then spend the rest of the
    # round completing.
    #
    # The window and straddle_first_leg_max_price are one decision, not two.
    # A minute in, spot has barely left the strike and both sides still
    # price near 0.50, so a 0.25 ceiling here would point the opener at the
    # one stretch of the round where its entry price cannot occur -- which
    # is the dead-bot failure a 15s window used to cause, reintroduced from
    # the other end. The ceiling is loosened to match (see below); paying
    # 0.40 for four minutes of completion time is the trade being made.
    straddle_entry_window_s: float = 60.0
    # Optional per-leg sanity ceiling. 1.0 means no ceiling: every round in
    # the window is taken at whatever price is on offer, deliberately,
    # because this strategy's premise is that direction does not matter and
    # skipping rounds on a price judgement is a different strategy. Set
    # below 1.0 only if you want to opt into refusing a leg priced above a
    # level you have decided is too rich.
    straddle_max_leg_price: float = 1.0
    # Off by default. When enabled, a round is only taken if the WORSE of
    # the two possible outcomes still returns at least this fraction of the
    # total staked -- i.e. a hard requirement that the round be a mechanical
    # arbitrage before it is touched. Left off by default because that
    # requirement is what would make the bot refuse most rounds; whether
    # round-open mispricing is real enough to trade without it is exactly
    # what --calibration-report against real results answers, not a formula
    # decided in advance.
    straddle_require_positive_worst_case: bool = False
    straddle_min_worst_case_return: float = 0.0

    # --- Legging in: the two sides are bought at DIFFERENT times ----------
    # The prices of the two sides sum to about 1.00 at any given instant, so
    # a pair bought simultaneously is almost never profitable both ways.
    # Bought at different moments it can be: UP at 0.25 while spot is falling
    # and DOWN at 0.25 twenty seconds later once it has bounced never coexist
    # in the book, but the two fills together still return 4x on each side of
    # a round that cost 2 stakes. Prices summing to 1.00 constrains one
    # instant, not one round.
    #
    # The most the OPENING leg may cost. This governs the first leg only:
    # what the second holds out for is derived from what the first actually
    # filled at, not from this number (_completion_is_worth_waiting_out).
    # Which side it happens to be is never considered -- whichever of UP and
    # DOWN is showing a price under this is the one that gets bought.
    #
    # 0.25 -- a 4x payout -- is the price this strategy would LIKE, and it
    # is still what the second leg is measured against once a leg fills
    # there. It is the wrong ceiling for a first minute, though: 0.25 does
    # not exist that early, so a bot holding out for it opens nothing. 0.40
    # is what an early book actually offers.
    #
    # The number matters more than it looks. After filling at p, the other
    # side stays worth buying all the way up to (1 - p), so an opener at
    # 0.40 leaves 0.60 of room to complete in, while one at 0.49 leaves
    # almost none and stands a real chance of being stranded. That room,
    # against the four minutes the shortened entry window leaves to use it,
    # is the whole reason 0.40 is tolerable and 0.49 is not.
    straddle_first_leg_max_price: float = 0.40
    # Opening a round is limited to straddle_entry_window_s. COMPLETING one
    # is not: a hedge that locks the profit in at t=120s is worth exactly
    # what one at t=9s is worth, and refusing it would leave a naked
    # directional bet on the book for no reason. The second leg is hunted
    # until this many seconds remain, at which point the search stops and
    # straddle_force_hedge decides what happens to the open leg.
    straddle_hedge_deadline_s: float = 30.0
    # At that deadline, buy the other side at whatever it costs. The round is
    # then usually a small loss instead of a coin flip on the whole stake --
    # the guarantee is already gone by this point, and the only question left
    # is whether the position stays all-or-nothing. Turn off only to hold the
    # open leg to settlement as an outright directional bet.
    straddle_force_hedge: bool = True

    # --- Last minute (buy whichever side the book has already picked) -----
    # A third entry strategy, and the simplest thing in this file. It reads
    # no model, no volatility, no buffer and no trend. With a minute left it
    # looks at the two asks, buys the dearer one -- the side the book is
    # calling the winner -- and does nothing else for the rest of the round.
    #
    # The premise is that a price is a forecast, and in the last minute of a
    # five-minute round it is a forecast with almost no time left in which to
    # be wrong. That is a claim about this venue's late pricing, not about
    # BTC, and --calibration-report's favourite-longshot table is the thing
    # that settles it. Nothing here is derived from the model, so nothing
    # here can be defended by the model either.
    last_minute: bool = False
    # When the hunt opens, in seconds before settlement.
    last_minute_start_s: float = 60.0
    # The price the leading side must show to be bought on sight.
    #
    # Above 0.5 by construction: at or below it the "dearer side" test and
    # the floor would say the same thing, the floor could never fail, and
    # the fallback below would be dead code wearing the costume of a setting.
    last_minute_price_floor: float = 0.75
    # Never pay more than this for the leader. 1.0 means NO ceiling, which
    # is the rule as originally specified and the default here.
    #
    # It exists because the floor has nothing above it, and that asymmetry is
    # where this strategy bleeds. The floor is a payout question wearing a
    # price: at 0.75 a win pays about 33% and 3 wins cover a loss, but at
    # 0.97 a win pays about 3% and it takes 32. Those are not the same trade,
    # and the second one is what a round that is ALREADY DECIDED at 55
    # seconds looks like -- so the dear fills are exactly the ones with the
    # least left to win and the most already priced in.
    #
    # Deliberately a price and not a min_win_return: the return floor is the
    # model path's instrument and it derives its cap from each market's fee,
    # which is the right shape for a strategy that reasons in probabilities.
    # This one reasons in nothing but the quote, so its ceiling is a quote.
    #
    # 0.90 (about 9 wins per loss) or 0.85 (about 6) are the settings worth
    # trying; both are still above the 0.75 floor, so the band they leave --
    # 0.75 to the ceiling -- is where this profile does its work. Below the
    # floor the ceiling can never bind, so it does not touch the fallback.
    last_minute_max_price: float = 1.0
    # Below this many seconds the floor is dropped and the leader is bought
    # at whatever it costs.
    #
    # Not a relaxation of the rule -- the rest of it. The floor only fails to
    # clear while the two sides are still close, which is to say while the
    # round is genuinely undecided; and in exactly that case the dearer side
    # is both the best read available AND cheap, because "no side reached
    # 0.75" means the thing being bought is under 0.75 by definition. Holding
    # out past this point forfeits the round waiting for a price that the
    # book has already declined to print.
    last_minute_fallback_s: float = 45.0
    # Stop trying. A MARKET order needs a book to still be there, and the
    # last few seconds of a round are when it is not.
    last_minute_deadline_s: float = 5.0
    # Stake per round, as a fraction of bankroll. One side, one order, one
    # round -- so unlike a straddle leg this is the whole commitment to the
    # round rather than half of it.
    last_minute_stake_pct: float = 0.05

    # --- Claiming (background, non-blocking) --------------------------
    # A win must be claimed (on-chain redemption) before its proceeds are
    # real, spendable balance, and that can take anywhere from about a
    # second to roughly a minute. The trading loop must not sit still for
    # that: it keeps scanning for the next round's open immediately, while
    # a dedicated background worker retries the claim and polls its status
    # on this cadence, independent of poll_interval_s, until it lands.
    claim_poll_interval_s: float = 1.0
    claim_timeout_s: float = 90.0

    # --- Plumbing ----------------------------------------------------------
    # --- Timing (previously hardcoded inside the loop) ------------------
    clock_resync_s: float = 300.0        # re-sync the server clock this often
    settle_grace_s: float = 2.0          # wait after end before settling
    settle_timeout_s: float = 600.0      # give up settling and log the gap
    drain_timeout_s: float = 600.0       # wait for an open position on exit
    drain_poll_s: float = 5.0
    prune_after_s: float = 3600.0        # forget rounds this long past expiry
    vol_cache_s: float = 60.0
    error_backoff_max_s: float = 30.0
    # How long --preflight waits for a signed request to be ACCEPTED before
    # it gives up, and how often it retries while waiting.
    #
    # On shared egress the outbound address is not known until the process is
    # running and can change on any restart, so the address that has to be in
    # Binance's allowlist cannot be added in advance. Without this, preflight
    # fails in the first second of boot, the deploy dies, and the address is
    # gone before it can be pasted anywhere. Waiting turns that race into a
    # window: preflight prints the address, then keeps knocking until the
    # allowlist entry lands.
    #
    # Bounded rather than infinite on purpose -- an unbounded wait is a paid
    # worker sitting idle forever on a key that may simply be wrong. 0
    # disables the wait and fails on the first refusal.
    auth_wait_timeout_s: float = 0.0
    auth_wait_poll_s: float = 5.0

    # --- Tolerances and paging (previously hardcoded) -------------------
    round_duration_tolerance: float = 0.10    # fraction of the target length
    quote_consistency_tolerance: float = 0.10
    market_list_limit: int = 50
    # 50 could silently miss an older settlement and leave a position
    # looking unresolved when the venue had already settled it.
    settled_history_limit: int = 100

    db_path: str = "btc5m_journal.db"
    # Print the calibration report to the log every N settled trades. On a
    # hosted worker the journal sits on a disk you cannot easily read, so
    # without this the only record is one you have to go and fetch.
    report_every: int = 0
    poll_interval_s: float = 2.0
    recv_window_ms: int = 5000
    http_timeout_s: float = 10.0
    paper_start_bankroll: float = 100.0
    endpoints: tuple[tuple[str, str], ...] = ()
    profile_name: str = "custom"

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("API key and secret are required")
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0 < self.max_stake_pct <= 0.25:
            raise ValueError("max_stake_pct must be in (0, 0.25]")
        if self.hard_stake_multiple < 1.0:
            raise ValueError("hard_stake_multiple must be >= 1.0")
        if not 0 < self.hard_stake_ceiling <= 0.5:
            raise ValueError("hard_stake_ceiling must be in (0, 0.5]")
        if not 0 < self.min_edge < 1:
            raise ValueError("min_edge must be in (0, 1)")
        if self.min_edge_ratio < 0:
            raise ValueError("min_edge_ratio must be non-negative")
        if self.min_buffer_sigmas < 0:
            raise ValueError("min_buffer_sigmas must be non-negative")
        if not 0 < self.scale_in_initial_pct <= 1.0:
            raise ValueError("scale_in_initial_pct must be in (0, 1]")
        if not 0 < self.max_blended_price < 1.0:
            raise ValueError("max_blended_price must be in (0, 1)")
        if not (self.min_entry_price <= self.max_blended_price
                <= self.max_entry_price):
            # Above the band it can never bind; below it, no position could
            # ever satisfy it and top-ups would never happen. Either way the
            # setting silently does nothing, which is worse than an error.
            raise ValueError(
                f"max_blended_price {self.max_blended_price} must lie within "
                f"the entry band {self.min_entry_price}-{self.max_entry_price}")
        if self.min_win_return < 0:
            raise ValueError("min_win_return must be non-negative")
        if self.min_win_return > 0:
            # A floor the band can never satisfy is a bot that never trades
            # and never says why, which is the worst of the three outcomes.
            cap = max_price_for_return(self.min_win_return, self.fee_bps)
            if cap <= self.min_entry_price:
                raise ValueError(
                    f"min_win_return {self.min_win_return} caps the fill "
                    f"price at {cap:.4f} at {self.fee_bps} bps, which is at "
                    f"or below min_entry_price {self.min_entry_price}: no "
                    f"price could ever satisfy both")
        if not 0 < self.assumed_spread_pct < 1.0:
            raise ValueError("assumed_spread_pct must be in (0, 1)")
        # Empty is valid: it means "no restriction, discover every market".
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must not contain duplicates")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")
        if not 0 <= self.reserve_pct < 1:
            raise ValueError("reserve_pct must be in [0, 1)")
        if not 0 < self.round_duration_tolerance < 1.0:
            raise ValueError("round_duration_tolerance must be in (0, 1)")
        if self.settled_history_limit < 1:
            raise ValueError("settled_history_limit must be positive")
        if self.fill_confirm_attempts < 1:
            raise ValueError("fill_confirm_attempts must be >= 1")
        if not 0 < self.min_fill_fraction <= 1.0:
            raise ValueError("min_fill_fraction must be in (0, 1]")
        if self.fill_confirm_delay_s <= 0:
            raise ValueError("fill_confirm_delay_s must be positive")
        for name in ("clock_resync_s", "settle_grace_s", "settle_timeout_s",
                     "drain_timeout_s", "drain_poll_s", "prune_after_s",
                     "vol_cache_s", "error_backoff_max_s", "auth_wait_poll_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.auth_wait_timeout_s < 0:
            raise ValueError("auth_wait_timeout_s must be non-negative")
        if self.trend_min_run < 1:
            raise ValueError("trend_min_run must be >= 1")
        if self.trend_max_run < self.trend_min_run:
            raise ValueError("trend_max_run must be >= trend_min_run")
        if self.trend_min_z < 0:
            raise ValueError("trend_min_z must be non-negative")
        if self.trend_min_impulse <= 0:
            raise ValueError("trend_min_impulse must be positive")
        if not 0 < self.trend_decay_floor <= 1.0:
            # Above 1.0 it would demand acceleration on every block, which
            # no real move sustains; at or below 0 it could never fire.
            raise ValueError("trend_decay_floor must be in (0, 1]")
        if self.trend_min_rounds_left < 0:
            raise ValueError("trend_min_rounds_left must be non-negative")
        if not 0 <= self.trend_min_efficiency <= 1:
            raise ValueError("trend_min_efficiency must be in [0, 1]")
        if self.trend_stake_multiple < 1.0:
            # Below 1.0 the "boost" would shrink the stake on exactly the
            # setups the profile is built to press, which is not a tuning
            # choice but a sign inversion.
            raise ValueError("trend_stake_multiple must be >= 1.0")
        if self.trend_early_entry_s < 0:
            raise ValueError("trend_early_entry_s must be non-negative")
        if self.trend_follow:
            # The run is counted in round-length blocks, so the window has to
            # hold at least trend_max_run of them or the ceiling can never be
            # reached and the fade rule silently never fires.
            needed = self.trend_max_run * self.round_seconds / 60.0
            if self.trend_lookback_min < needed:
                raise ValueError(
                    f"trend_lookback_min {self.trend_lookback_min} is shorter "
                    f"than trend_max_run x round_seconds ({needed:.0f} min); "
                    f"the run ceiling could never be reached")
        if self.report_every < 0:
            raise ValueError("report_every must be non-negative")
        if self.use_fat_tails and self.tail_df_floor <= 2.0:
            raise ValueError("tail_df_floor must exceed 2 for finite variance")
        if self.calibration_z_halt >= 0:
            raise ValueError("calibration_z_halt must be negative")
        if self.sigma_window_min > self.vol_lookback_min:
            raise ValueError("sigma_window_min must not exceed vol_lookback_min")
        if self.entry_window_end_s >= self.entry_window_start_s:
            raise ValueError("entry_window_end_s must be < entry_window_start_s")
        if not 0 < self.min_entry_price < self.max_entry_price < 1:
            raise ValueError("require 0 < min < max < 1 entry price")
        if self.vol_floor_annual <= 0:
            raise ValueError("vol_floor_annual must be positive")
        if not 0 <= self.fee_bps < 10_000:
            raise ValueError("fee_bps must be in [0, 10000)")
        if not 1 <= self.max_slippage_bps <= 10_000:
            raise ValueError("max_slippage_bps must be in [1, 10000]")
        if self.round_seconds <= 0:
            raise ValueError("round_seconds must be positive")
        if self.min_stake_usdt < 0.5:
            raise ValueError("min_stake_usdt below 0.50 is not plausible")
        if self.account_type not in ("AUTO", "SPOT", "FUNDING"):
            raise ValueError("account_type must be AUTO, SPOT or FUNDING")
        if self.funding_source not in ("AUTO", "MPC", "CEX"):
            raise ValueError("funding_source must be AUTO, MPC or CEX")
        if not 0 < self.straddle_stake_pct <= 0.25:
            raise ValueError("straddle_stake_pct must be in (0, 0.25]")
        if self.straddle_entry_window_s <= 0:
            raise ValueError("straddle_entry_window_s must be positive")
        if not 0 < self.straddle_max_leg_price <= 1.0:
            raise ValueError("straddle_max_leg_price must be in (0, 1]")
        if not 0 < self.straddle_first_leg_max_price < 0.5:
            # At 0.5 the first leg's payout only just covers an equal-sized
            # pair, leaving no room at all for the second leg to be worth
            # buying -- the strategy needs the first fill to be genuinely
            # cheap, not merely the better half of a coin flip.
            raise ValueError(
                "straddle_first_leg_max_price must be in (0, 0.5)")
        if self.straddle_hedge_deadline_s < 0:
            raise ValueError("straddle_hedge_deadline_s must be "
                             "non-negative")
        if self.straddle_min_worst_case_return < 0:
            raise ValueError(
                "straddle_min_worst_case_return must be non-negative")
        if self.straddle and self.scale_in:
            # Scale-in tops a position up toward the Kelly stake for a
            # rising model probability. The straddle strategy has no model
            # probability -- there is nothing for scale-in to top up toward.
            raise ValueError(
                "straddle and scale_in cannot both be enabled")
        if not 0 < self.last_minute_stake_pct <= 0.25:
            raise ValueError("last_minute_stake_pct must be in (0, 0.25]")
        if not self.last_minute_price_floor < self.last_minute_max_price <= 1.0:
            # At or below the floor no price could satisfy both, so the
            # primary branch could never fire and the profile would quietly
            # become fallback-only -- a different strategy wearing this
            # one's name. Above 1.0 it is not a price.
            raise ValueError(
                f"last_minute_max_price ({self.last_minute_max_price}) must "
                f"be above last_minute_price_floor "
                f"({self.last_minute_price_floor}) and at most 1.0")
        if not 0.5 < self.last_minute_price_floor < 1.0:
            # See the field comment: at or below 0.5 the floor can never
            # fail, so the fallback can never fire.
            raise ValueError("last_minute_price_floor must be in (0.5, 1)")
        if self.last_minute_deadline_s < 0:
            raise ValueError("last_minute_deadline_s must be non-negative")
        if not (self.last_minute_deadline_s < self.last_minute_fallback_s
                <= self.last_minute_start_s):
            # Ordered, or one of the three silently does nothing: a fallback
            # later than the start never gets a chance to hold the floor up,
            # and one earlier than the deadline never gets a chance to drop
            # it. Both read as configuration and behave as nothing.
            raise ValueError(
                f"require last_minute_deadline_s "
                f"({self.last_minute_deadline_s}) < last_minute_fallback_s "
                f"({self.last_minute_fallback_s}) <= last_minute_start_s "
                f"({self.last_minute_start_s})")
        if self.last_minute and self.straddle:
            # Two entry strategies behind one dispatch. Enabling both would
            # silently run whichever branch happens to be tested first.
            raise ValueError("straddle and last_minute cannot both be enabled")
        if self.last_minute and self.scale_in:
            # Scale-in tops a position up toward the Kelly stake for a RISING
            # model probability. This strategy has no model probability --
            # there is nothing for a top-up to aim at.
            raise ValueError(
                "last_minute and scale_in cannot both be enabled")
        if self.claim_poll_interval_s <= 0:
            raise ValueError("claim_poll_interval_s must be positive")
        if self.claim_timeout_s <= 0:
            raise ValueError("claim_timeout_s must be positive")

    @property
    def symbol(self) -> str:
        """
        A representative single market.

        Used only for informational purposes where exactly one symbol is
        needed -- preflight's spot/volatility probe, and PredictionClient's
        fallback when a venue settlement feed cannot be resolved to a
        Binance ticker. Does NOT restrict what gets traded; that is `symbols`
        (or its absence, which means "every market").
        """
        return self.symbols[0] if self.symbols else "BTCUSDT"

    @property
    def hard_max_stake_pct(self) -> float:
        """
        Ceiling for the venue-minimum override. Never below max_stake_pct.
        """
        return min(max(self.max_stake_pct * self.hard_stake_multiple,
                       self.max_stake_pct), self.hard_stake_ceiling)

    def ep(self, name: str) -> tuple[str, str]:
        """(method, path) for a named endpoint."""
        return (dict(self.endpoints) or DEFAULT_ENDPOINTS)[name]


PROFILES: dict[str, dict] = {
    # Big wins, small losses. Buys cheap contracts, so it LOSES MOST ROUNDS by
    # construction; the winners have to be large enough to pay for them.
    "convex": {"max_entry_price": 0.35, "min_entry_price": 0.05,
                   "min_edge": 0.02, "min_edge_ratio": 0.30,
                   "max_stake_pct": 0.02, "entry_window_start_s": 280,
                   "entry_window_end_s": 30, "max_consecutive_losses": 60,
                   # Longshots: many small losses, so the default fits.
                   "daily_loss_limit_pct": 0.20, "assumed_spread_pct": 0.10,
                   # Tail probabilities are the least reliable part of the
                   # model, and this profile lives on them, so size more
                   # cautiously than a mid-price strategy would.
                   "kelly_fraction": 0.20,
                   "min_liquidity": 0.0, "max_rounds_per_day": 200,
                   "paper_start_bankroll": 100.0,
                   # No floor needed: the band's own top, 0.35, already pays
                   # about 180% on a win. Stated rather than inherited so a
                   # change to the default cannot silently reshape this.
                   "min_win_return": 0.0,
                   # Inert here unless scale_in is enabled; sized to this
                   # profile's own band (0.05-0.35), not buffer's.
                   "max_blended_price": 0.3},
    # Symmetric: trades anywhere it finds an edge. Higher hit rate, smaller
    # payoffs, and correspondingly larger individual losses.
    "balanced": {"max_entry_price": 0.90, "min_entry_price": 0.10,
                     "min_edge": 0.04, "min_edge_ratio": 0.10,
                     "max_stake_pct": 0.05, "entry_window_start_s": 150,
                     "entry_window_end_s": 25, "max_consecutive_losses": 10,
                     "daily_loss_limit_pct": 0.20, "assumed_spread_pct": 0.06,
                   "kelly_fraction": 0.25,
                     "min_liquidity": 0.0, "max_rounds_per_day": 200,
                     "paper_start_bankroll": 100.0,
                     # Symmetric by design: it trades wherever an edge is,
                     # including the expensive end, so a return floor would
                     # amputate half of what this profile is for.
                     "min_win_return": 0.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.10-0.90), not buffer's.
                     "max_blended_price": 0.8},
    # For small accounts, where a percentage cap would fall under the venue's
    # order minimum and the bot would simply never trade. Targets the 0.40-0.75
    # band -- roughly 30-150% return per win. Those are large PERCENTAGE wins
    # that merely look small in dollars on a small balance.
    #
    # 20% of bankroll per round is deliberately aggressive and is the price of
    # trading a small account at all. It is survivable (a loss is bounded and
    # 20 consecutive losses still leave ~4% of the balance) but it is NOT the
    # Kelly-optimal fraction, and scaling stake up after wins compounds both
    # directions. Move to "balanced" once the balance clears ~30 USDT.
    # Buy the favourite, late in the round, while the return is still worth
    # having. Derived from a rule of "return >= 25%", i.e. price <= 0.80.
    #
    # This is the OPPOSITE side of the market from "convex". Prediction and
    # betting markets frequently show a favourite-longshot bias, in which
    # longshots are overpriced and favourites underpriced -- if that holds
    # here, this profile is on the right side of it and convex is on the
    # wrong one. `--calibration-report` measures which, from real fills.
    #
    # Entering late is a genuine information edge, not superstition: with
    # less time left, the same price move is far more decisive, so the
    # model's probability is sharper. The cost is a thinner book.
    "favorite": {"max_entry_price": 0.80, "min_entry_price": 0.55,
                     "min_edge": 0.03, "min_edge_ratio": 0.05,
                     "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
                     "daily_loss_limit_pct": 0.30, "assumed_spread_pct": 0.04,
                   "kelly_fraction": 0.25,
                     "min_liquidity": 0.0, "max_rounds_per_day": 200,
                     "entry_window_start_s": 120, "entry_window_end_s": 20,
                     "max_consecutive_losses": 10, "paper_start_bankroll": 25.0,
                     # The 0.80 band top already implies ~25% at a 2% fee, so
                     # a floor here would only duplicate the band -- and at a
                     # market with a higher fee it would start rejecting
                     # trades this profile was built to take.
                     "min_win_return": 0.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.55-0.80), not buffer's.
                     "max_blended_price": 0.72},
    # YOUR METHOD, encoded. Wait for a buffer to open up, back the side it
    # favours, press it while the market has inertia -- and refuse any price
    # whose win is too small to be worth the loss it risks.
    #
    # THE RETURN FLOOR IS WHAT SHAPES THIS PROFILE
    # --------------------------------------------
    # An earlier version traded 0.80-0.95. Those are genuine edges, but at
    # 0.94 a win pays about 6%, so ONE loss erases sixteen wins and a day of
    # patient work is undone by a single round going the other way. The edge
    # test cannot see this: edge is measured in probability and the problem
    # is measured in money.
    #
    # min_win_return=0.25 says a win must pay at least a quarter of the
    # stake. At a 2% fee that caps the fill at about 0.797, so the band ends
    # there and roughly four wins cover a loss instead of sixteen.
    #
    # WHAT THAT COSTS, STATED PLAINLY
    # -------------------------------
    # Price and buffer move together: a 1.5-sigma buffer is a ~93% chance and
    # a market that has noticed will quote near 0.93, which this profile now
    # refuses. So the trades that remain are the ones where the buffer is
    # real but the BOOK HAS NOT CAUGHT UP -- the venue still quoting 0.75
    # while spot has already moved. That is the manual edge being encoded,
    # and there are fewer such rounds than there were cheap-looking 0.94s.
    # Expect materially fewer trades and larger individual wins.
    #
    # min_buffer_sigmas is 0.75 for the same reason. Demanding 1.5 sigmas
    # while capping the price at 0.80 asks for a 93% chance at a 79% price,
    # which almost never coexists; 0.75 sigmas is a ~77% chance, so a venue
    # quote at or under 0.797 is a live disagreement rather than a fantasy.
    "buffer": {"max_entry_price": 0.80, "min_entry_price": 0.55,
                   "min_edge": 0.012, "min_edge_ratio": 0.010,
                   # A win must pay at least 25% of the stake, after fees.
                   # This, not max_entry_price, is the binding ceiling: it
                   # tracks each market's own published fee instead of
                   # assuming one.
                   "min_win_return": 0.25,
                   # See the note above: the price cap and the buffer gate
                   # pull against each other, and 0.75 is where both can be
                   # satisfied often enough to produce trades.
                   "min_buffer_sigmas": 0.75,
                   "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
                   # A loss here costs a full 10% of bankroll, so the 20%
                   # default halted the day after TWO losses -- on 80% of
                   # days at an 85% win rate. The limit has to match the
                   # profile's own loss shape or it stops a healthy bot.
                   "daily_loss_limit_pct": 0.35,
                   # Thin books still destroy an edge measured in single
                   # points, but 0.02 was tuned for near-certainty fills at
                   # 0.90+. Inside the return floor's band the stakes are
                   # smaller relative to the book, so this was rejecting
                   # rounds whose price was fine. Loosened deliberately: it
                   # is a fill-quality gate, not a return gate, and bundling
                   # the two is what made the floor look like it was cutting
                   # trade count.
                   "max_price_impact": 0.05,
                   "assumed_spread_pct": 0.03,
                   "kelly_fraction": 0.25,
                   # 1000 USDT of resting depth is more than a 5-minute
                   # market typically shows, so this gate alone was capable
                   # of rejecting every round -- silently, and for a reason
                   # that has nothing to do with the price being good. It
                   # exists to avoid unfillable books, and 150 does that.
                   "min_liquidity": 150.0, "max_rounds_per_day": 250,
                   # A smaller first tranche leaves more room to add once the
                   # round has proven itself, so the top-up genuinely is the
                   # larger bet -- roughly 4x the opener rather than 1.5x.
                   # Total exposure is still bounded by Kelly, and by the
                   # blended-price cap below.
                   "scale_in": True, "scale_in_initial_pct": 0.25,
                   "scale_in_min_topup": 1.0,
                   # Inside the return floor's own ceiling on purpose. A
                   # top-up is bought at a HIGHER price than the opener, so
                   # the blend is the number that decides the payout, and
                   # letting it drift to 0.797 would spend the whole return
                   # budget on the last tranche.
                   "max_blended_price": 0.78,
                   # INERTIA, caught at its start rather than after the fact.
                   # trend_min_impulse is the trigger and does its work on
                   # the CURRENT block, so a move gets backed on its first
                   # thrust; trend_min_run=1 is what allows that. The run
                   # ceiling and the decay floor are the other half: a move
                   # that has already run five rounds, or that is shedding
                   # more than 45% of its size block over block, is bought at
                   # its worst price and is refused. trend_min_rounds_left=1
                   # asks the only question the entry actually poses -- does
                   # this survive the round I am entering?
                   "trend_follow": True,
                   "trend_min_impulse": 1.2, "trend_min_run": 1,
                   "trend_max_run": 5, "trend_decay_floor": 0.55,
                   "trend_min_rounds_left": 1.0, "trend_min_z": 0.8,
                   "trend_min_efficiency": 0.40,
                   "trend_stake_multiple": 1.5, "trend_early_entry_s": 90,
                   "trend_lookback_min": 30,
                   # Nearly the whole round. The return floor means the good
                   # price and the buffer rarely coexist for long, so the
                   # window has to be open when they do -- a narrow window
                   # turns "no trade was available" into "we were not
                   # looking", and those are not the same thing.
                   "entry_window_start_s": 270, "entry_window_end_s": 15,
                   "max_consecutive_losses": 6, "paper_start_bankroll": 25.0},
    "micro": {"max_entry_price": 0.75, "min_entry_price": 0.35,
                  "min_edge": 0.03, "min_edge_ratio": 0.06,
                  "max_stake_pct": 0.20, "min_stake_usdt": 1.0,
                  # 20% per trade means the 20% default halted after ONE loss.
                  # Even 45% halts after 2.25. At this stake fraction the
                  # stake cap and the daily limit are in genuine tension --
                  # that tension is forced by a 1.00 order minimum on a ~7
                  # balance, not chosen. Trading a bigger account is the only
                  # real fix; 55% is the least-bad compromise.
                  "daily_loss_limit_pct": 0.55,
                  "assumed_spread_pct": 0.05,
                  # A tiny balance forces a large stake fraction, so cap the
                  # hard ceiling tightly and let Kelly stay conservative.
                   "kelly_fraction": 0.25,
                  "min_liquidity": 0.0, "max_rounds_per_day": 200,
                  "entry_window_start_s": 200, "entry_window_end_s": 25,
                  "max_consecutive_losses": 10, "paper_start_bankroll": 7.0,
                  # A balance this small needs every trade it can get; a
                  # return floor on top of the band would leave it flat.
                  "min_win_return": 0.0,
                  # Inert here unless scale_in is enabled; sized to this
                  # profile's own band (0.35-0.75), not buffer's.
                  "max_blended_price": 0.65},
    # Buy BOTH sides in the first seconds of a round, but only when the two
    # payouts both beat what the pair costs -- see straddle_* on Config, and
    # _straddle_payouts_clear for the gate itself. 20% of bankroll per leg,
    # so ~40% committed per round. Deliberately large: at 2.5% a small
    # bankroll produced a per-leg stake under min_stake_usdt and the profile
    # silently never traded.
    "straddle": {"straddle": True, "straddle_stake_pct": 0.20,
                 # The first minute, and then four minutes to hedge in.
                 # Opening late is what strands legs; opening early is only
                 # possible at a price the early book offers, which is why
                 # this moves together with straddle_first_leg_max_price.
                 "straddle_entry_window_s": 60.0,
                 # No side is ever picked by price here, so these bands are
                 # left at their widest legal setting rather than inherited
                 # from another profile -- nothing below should silently
                 # reject a leg the straddle logic already screened.
                 "min_entry_price": 0.01, "max_entry_price": 0.99,
                 # Matched to straddle_stake_pct, not inherited. The straddle
                 # path never consults max_stake_pct -- it sizes off
                 # straddle_stake_pct directly -- so leaving this at 5% while
                 # each leg staked 20% made the config declare a cap the bot
                 # did not honour, and made --check-config and --preflight
                 # both report a limit that was never in force.
                 "max_stake_pct": 0.20, "min_edge": 0.02,
                 # 30% held back leaves 70% spendable, and one round needs
                 # 40% (two 20% legs). That capped the profile at a single
                 # live round no matter what max_concurrent_positions said.
                 # 10% leaves room for the second round the slot count is
                 # there for; a third is still refused for want of funds.
                 "reserve_pct": 0.10,
                 "min_edge_ratio": 0.0,
                 # A straddle's worst case per round is roughly one leg's
                 # stake (the other leg always pays something back), so 20%
                 # per leg means a bad round costs about 20% of bankroll. At
                 # the old 20% daily limit that halted the bot for the day
                 # after a SINGLE bad round; 50% leaves the 2.5-loss headroom
                 # every other profile has.
                 "daily_loss_limit_pct": 0.50,
                 "assumed_spread_pct": 0.10, "kelly_fraction": 0.25,
                 "min_liquidity": 0.0, "max_rounds_per_day": 400,
                 "paper_start_bankroll": 100.0, "min_win_return": 0.0,
                 "max_blended_price": 0.5,
                 # Never on by default for this profile: scale-in tops up
                 # toward a model probability this strategy does not have.
                 # Opt in explicitly (and only after reading why it is off)
                 # by overriding it back to True in your own config.
                 "scale_in": False,
                 # Two legs per round occupy two slots; four lets a second
                 # round's straddle open while the first is still settling.
                 "max_concurrent_positions": 4,
                 # THE gate. A round is entered only when both legs would pay
                 # back more than the pair cost together -- UP wins and the
                 # payout beats the whole stake, DOWN wins and it beats the
                 # whole stake -- so the round's direction stops mattering.
                 # That is a real condition, not a formality: it holds only
                 # while the two fee-adjusted prices sum to under 1.00, so
                 # most rounds are refused and the bot re-tests each one
                 # every poll until its window closes. Expect long stretches
                 # of no trades; --calibration-report and the periodic
                 # "no trade in N rounds" summary are how you tell that
                 # apart from a broken feed.
                 "straddle_require_positive_worst_case": True,
                 # 0.0 means "strictly more than the stake, by any margin".
                 # Raise it to demand a minimum locked-in return -- 0.01 for
                 # 1% of the pair, and correspondingly fewer rounds.
                 "straddle_min_worst_case_return": 0.0},
    # ONE RULE. With a minute left, buy whichever side is dearer -- the one
    # the book has already picked -- provided it is quoted at 0.75 or better.
    # Inside the last 45 seconds, buy it whatever it costs. Nothing else is
    # consulted and nothing else is done: no model probability, no edge test,
    # no buffer, no trend, no scale-in, no second leg.
    #
    # WHAT THE TWO HALVES COST, STATED PLAINLY
    # ----------------------------------------
    # They are not the same trade, and the expensive one is not the fallback.
    # A 0.75 fill pays about 33% on a win at a 2% fee, so 3 wins cover a
    # loss. But the floor has NO ceiling above it, and a round already
    # decided at 55 seconds quotes 0.97 -- which this profile buys, for about
    # 3% on a win, where it takes 32 wins to cover one loss. That is where
    # this strategy can bleed, and it is the primary branch, not the
    # fallback: the fallback only ever fires below 0.75 and therefore only
    # ever buys the CHEAP end.
    #
    # No max_entry_price is imposed to stop that, deliberately -- the band is
    # left at its widest legal setting so nothing downstream quietly
    # reinstates a filter this profile was written to do without. The
    # favourite-longshot table in --calibration-report is what says whether
    # the venue's late favourites win often enough to pay for the dear ones.
    "lastminute": {"last_minute": True, "last_minute_stake_pct": 0.10,
                   "last_minute_start_s": 60.0,
                   "last_minute_price_floor": 0.75,
                   # No ceiling, stated rather than inherited. This is the
                   # rule as asked for: above the floor, price is not a
                   # reason to refuse. Set it to 0.90 or 0.85 to stop buying
                   # rounds the book has already finished pricing.
                   "last_minute_max_price": 1.0,
                   "last_minute_fallback_s": 45.0,
                   "last_minute_deadline_s": 5.0,
                   # Matched to last_minute_stake_pct, not inherited. The
                   # last-minute path never consults max_stake_pct -- it
                   # sizes off its own fraction directly -- so a disagreeing
                   # value would make --check-config and --preflight both
                   # report a cap that is not in force, which is exactly the
                   # lie the straddle profile had to be corrected for.
                   "max_stake_pct": 0.10,
                   # Widest legal band. No side is ever chosen on a price
                   # judgement here beyond the floor itself, so nothing below
                   # should silently reject a leader the rule already picked.
                   "min_entry_price": 0.01, "max_entry_price": 0.99,
                   # Inert on this path -- there is no edge computation to
                   # threshold -- but stated rather than inherited so a
                   # change to the defaults cannot reshape this profile.
                   "min_edge": 0.02, "min_edge_ratio": 0.0,
                   "min_win_return": 0.0, "min_buffer_sigmas": 0.0,
                   # A loss costs a full 10% of bankroll, so the 20% default
                   # would halt the day after two of them. 35% leaves the
                   # 3.5-loss headroom the other single-sided profiles have.
                   "daily_loss_limit_pct": 0.35,
                   # Tight: entries happen in the last minute, when the two
                   # sides have separated and the leader's quote is firm.
                   "assumed_spread_pct": 0.03,
                   "kelly_fraction": 0.25,
                   # A late book is thinner than a mid-round one, and this
                   # profile has no way to wait for a better one, so a
                   # liquidity floor here would simply refuse rounds without
                   # improving the fills it does get. The venue's own FOK
                   # kill is the real protection; see _place_leg.
                   "min_liquidity": 0.0, "max_rounds_per_day": 300,
                   "paper_start_bankroll": 100.0,
                   # Inert unless scale_in is enabled, which validation
                   # forbids for this profile; sized to its own band.
                   "max_blended_price": 0.5,
                   # Never on: there is no model probability to top up
                   # toward, which is why Config rejects the combination.
                   "scale_in": False,
                   # One position per round and one round per market, so two
                   # slots is two live markets, not two bets on one.
                   "max_concurrent_positions": 2,
                   # 30% held back leaves 70% spendable against a 10% stake,
                   # which funds both slots with room to spare.
                   "reserve_pct": 0.30},
}


# --------------------------------------------------------------------------
# Configuration file and hot reload
# --------------------------------------------------------------------------

# The default strategy, declared once. Previously six literals across four
# files each carried their own copy of this, which is precisely how a default
# drifts: change five and the sixth silently disagrees.
DEFAULT_PROFILE = "lastminute"

# Fields that cannot change while the bot is running. Swapping any of these
# mid-flight would leave the process in a state that does not match what it
# already did: a different journal would split one session's record across two
# files, a different key would sign with credentials the open position was not
# opened under. They are read once at startup and ignored on reload.
IMMUTABLE_FIELDS = frozenset({
    "api_key", "api_secret", "db_path", "endpoints", "profile_name",
})

# `live` is reloadable but NOT applied instantly. Switching mode while a
# position is open is incoherent in both directions: a paper position has no
# real order behind it, so live settlement would look for a venue position
# that never existed; and a real position flipped to paper stops being
# tracked while its settlement is simulated. The Trader therefore defers a
# mode change until it is flat, and resets the bankroll baseline on the swap.
DEFERRED_FIELDS = frozenset({"live"})

CONFIG_SCHEMA_NOTE = (
    "Every setting lives here. 'profiles' holds the named strategies, "
    "'active_profile' selects one, and 'overrides' is applied on top of it. "
    "The file is re-read whenever it changes on disk -- no restart needed. "
    "Fields listed in immutable_fields are fixed at startup."
)


def default_config_document(active_profile: str = DEFAULT_PROFILE) -> dict:
    """The full configuration as a plain document, ready to serialise."""
    base = {f.name: f.default for f in dataclasses.fields(Config)
            if f.default is not dataclasses.MISSING}
    # Every immutable field is excluded, not just the secrets. Emitting them
    # would make the file warn "these were ignored" on every single reload,
    # training the reader to ignore a warning that matters when it is real.
    for name in IMMUTABLE_FIELDS:
        base.pop(name, None)
    base = {k: (list(v) if isinstance(v, tuple) else v)
            for k, v in base.items()}
    return {
        "_note": CONFIG_SCHEMA_NOTE,
        "_immutable_fields": sorted(IMMUTABLE_FIELDS),
        "active_profile": active_profile,
        "defaults": base,
        "profiles": {name: dict(values) for name, values in PROFILES.items()},
        "overrides": {},
        "endpoints": {k: list(v) for k, v in DEFAULT_ENDPOINTS.items()},
    }


def _coerce(name: str, value: object) -> object:
    """Match a JSON value to the dataclass field's declared type."""
    hints = {f.name: f.type for f in dataclasses.fields(Config)}
    declared = str(hints.get(name, ""))
    if "tuple" in declared and isinstance(value, list):
        return tuple(tuple(x) if isinstance(x, list) else x for x in value)
    if "int" in declared and "float" not in declared and isinstance(value, float):
        if value != int(value):
            raise ValueError(f"{name} must be a whole number, got {value}")
        return int(value)
    return value


def build_config(document: dict, *, api_key: str, api_secret: str,
                 live: bool | None, db_path: str,
                 profile: str | None = None,
                 overrides: dict | None = None) -> Config:
    """
    Assemble a Config from a configuration document.

    Layering is explicit and one-directional: defaults, then the selected
    profile, then the document's overrides, then any command-line overrides.
    Everything passes through a single mapping so a key can never be supplied
    twice.
    """
    if not isinstance(document, dict):
        raise TypeError("configuration must be a JSON object")

    profiles = document.get("profiles") or {}
    name = profile or document.get("active_profile") or DEFAULT_PROFILE
    if name not in profiles:
        raise ValueError(
            f"unknown profile {name!r}; available: {sorted(profiles) or 'none'}")

    settings: dict = {}
    for layer in (document.get("defaults") or {}, profiles[name],
                  document.get("overrides") or {}, overrides or {}):
        for key, value in layer.items():
            if key.startswith("_"):
                continue
            settings[key] = value

    known = {f.name for f in dataclasses.fields(Config)}
    unknown = set(settings) - known
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")

    # A pinned value (CLI flag or environment) wins over the file and is not
    # hot-reloadable; without a pin the file governs and can be changed live.
    pinned_live = live is not None
    settings = {k: _coerce(k, v) for k, v in settings.items()
                if k not in IMMUTABLE_FIELDS}
    if pinned_live:
        settings["live"] = live
    elif "live" not in settings:
        settings["live"] = False

    endpoints = dict(DEFAULT_ENDPOINTS)
    for key, value in (document.get("endpoints") or {}).items():
        if key not in endpoints:
            raise ValueError(f"unknown endpoint {key!r}")
        if isinstance(value, str):
            endpoints[key] = (endpoints[key][0], value)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            method = str(value[0]).upper()
            if method not in ("GET", "POST", "PUT", "DELETE"):
                raise ValueError(f"bad HTTP method for {key}: {value[0]}")
            endpoints[key] = (method, str(value[1]))
        else:
            raise ValueError(f"{key}: expected a path or [method, path]")

    settings.update(api_key=api_key, api_secret=api_secret,
                    db_path=db_path, profile_name=name,
                    endpoints=tuple(endpoints.items()))
    return Config(**settings)


class ConfigStore:
    """
    Holds the live configuration and reloads it when the file changes.

    Reload is atomic and fail-safe: the new document is parsed and a Config is
    fully constructed before anything is swapped, so a malformed or invalid
    file leaves the running bot on its last good configuration rather than
    crashing it mid-position. Immutable fields are ignored on reload and
    reported, so an edit that appears to take effect but cannot is visible
    rather than silent.
    """

    def __init__(self, path: str | None, *, api_key: str, api_secret: str,
                 live: bool | None, db_path: str,
                 profile: str | None = None,
                 overrides: dict | None = None) -> None:
        self._path = path
        self._identity = {"api_key": api_key, "api_secret": api_secret,
                              "live": live, "db_path": db_path, "profile": profile,
                              "overrides": overrides or {}}
        self._mtime: float | None = None
        self._document = self._read()
        self._current = build_config(self._document, **self._as_kwargs())
        self.reload_count = 0

    def _as_kwargs(self) -> dict:
        d = dict(self._identity)
        return {"api_key": d["api_key"], "api_secret": d["api_secret"],
                    "live": d["live"], "db_path": d["db_path"],
                    "profile": d["profile"], "overrides": d["overrides"]}

    def _read(self) -> dict:
        if not self._path:
            return default_config_document()
        with open(self._path, encoding="utf-8") as fh:
            document = json.load(fh)
        self._mtime = os.path.getmtime(self._path)
        return document

    @property
    def current(self) -> Config:
        return self._current

    @property
    def path(self) -> str | None:
        return self._path

    def changed_on_disk(self) -> bool:
        if not self._path:
            return False
        try:
            return os.path.getmtime(self._path) != self._mtime
        except OSError as exc:
            # Expected transiently: many editors replace a file rather than
            # writing in place, so it can vanish for an instant. Logged so a
            # permanently missing file is visible rather than looking like
            # "nothing changed" forever.
            LOG.debug("Config file not readable right now: %s", exc)
            return False

    def maybe_reload(self) -> bool:
        """
        Re-read the file if it changed. Returns True when the config swapped.

        Never raises: a bad edit is reported and the previous configuration
        stays in force.
        """
        if not self.changed_on_disk():
            return False
        try:
            document = self._read()
            candidate = build_config(document, **self._as_kwargs())
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            LOG.error("Config reload REJECTED, keeping the previous one: %s",
                      exc)
            return False

        ignored = self._ignored_edits(document)
        if ignored:
            LOG.warning("These settings cannot change while running and were "
                        "ignored: %s. Restart to apply them.",
                        ", ".join(sorted(ignored)))

        changes = self._diff(self._current, candidate)
        self._document, self._current = document, candidate
        self.reload_count += 1
        if changes:
            LOG.info("Config reload #%d (%d change(s)): %s",
                     self.reload_count, len(changes),
                     "; ".join(changes[:8]))
        else:
            LOG.info("Config file changed but no effective setting differed")
        return True

    @property
    def live_is_pinned(self) -> bool:
        """True when a CLI flag or environment variable fixed the mode."""
        return self._identity["live"] is not None

    def _ignored_edits(self, document: dict) -> set[str]:
        present: set[str] = set()
        for layer in (document.get("defaults") or {},
                      document.get("overrides") or {}):
            present |= set(layer)
        ignored = present & IMMUTABLE_FIELDS
        if "live" in present and self.live_is_pinned:
            ignored = ignored | {"live (pinned by --live/--paper or TRADING_MODE)"}
        return ignored

    @staticmethod
    def _diff(old: Config, new: Config) -> list[str]:
        out = []
        for f in dataclasses.fields(Config):
            if f.name in ("api_key", "api_secret", "endpoints"):
                continue
            a, b = getattr(old, f.name), getattr(new, f.name)
            if a != b:
                out.append(f"{f.name}: {a} -> {b}")
        return out


# --------------------------------------------------------------------------
# Domain types
# --------------------------------------------------------------------------


# `accountType` on place-order accepts ONLY these. Any other value the venue
# lists as a payment option (the prediction wallet itself) is NOT valid there,
# and passing one through produces -3026 with no field named.
CEX_ACCOUNT_TYPES = ("SPOT", "FUNDING")


class Side(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    @property
    def other(self) -> "Side":
        return Side.DOWN if self is Side.UP else Side.UP


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
    amount_out_shares: float
    price_impact: float
    fee_usdt: float


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

    def average_price(self, extra_stake: float, extra_price: float) -> float:
        """Blended fill price after adding another tranche."""
        total = self.committed_usdt + extra_stake
        if total <= 0:
            return extra_price
        shares = (self.committed_usdt / self.signal.fill_price
                  + extra_stake / extra_price)
        return total / shares if shares > 0 else extra_price


@dataclass(frozen=True)
class WalletRef:
    address: str
    wallet_id: str


class Shutdown(Exception):
    """A platform stop signal (SIGTERM) was received."""


class TradingHalted(Exception):
    """A risk limit tripped. Always fails closed."""


class ErrorKind(str, Enum):
    """
    What an API failure actually means.

    Classified from the venue's numeric code wherever possible. Matching on
    message substrings is fragile -- wording changes, locales differ, and a
    keyword list silently mis-files anything it does not recognise. The code
    is the structured field; the text is only a last resort.
    """

    SIZE = "SIZE"                    # order too small / not enough depth
    INSUFFICIENT_FUNDS = "FUNDS"     # balance cannot cover the order
    AUTH = "AUTH"                    # key, signature, permissions, IP
    TIMING = "TIMING"                # clock drift / recvWindow
    PARAMETER = "PARAMETER"          # malformed or missing parameter
    NOT_FOUND = "NOT_FOUND"
    GEO_BLOCKED = "GEO_BLOCKED"      # HTTP 451: server is in a restricted region
    UNKNOWN = "UNKNOWN"


# Venue codes observed or documented. Anything absent stays UNKNOWN, which
# callers must treat as "do not proceed" rather than "probably harmless".
ERROR_CODES: dict[int, ErrorKind] = {
    -9000: ErrorKind.INSUFFICIENT_FUNDS,
    -3026: ErrorKind.PARAMETER,
    -1022: ErrorKind.AUTH,
    -2014: ErrorKind.AUTH,
    -2015: ErrorKind.AUTH,
    -1002: ErrorKind.AUTH,
    -1021: ErrorKind.TIMING,
    -1102: ErrorKind.PARAMETER,
    -1104: ErrorKind.PARAMETER,
    -1121: ErrorKind.PARAMETER,
}

# Fallback only, when no numeric code is supplied.
_MESSAGE_HINTS: tuple[tuple[tuple[str, ...], ErrorKind], ...] = (
    (("enough", "insufficient balance", "insufficient funds"),
     ErrorKind.INSUFFICIENT_FUNDS),
    (("minimum", "too small", "min amount", "insufficient liquidity",
      "depth"), ErrorKind.SIZE),
    (("signature", "api-key", "api key", "permission", "unauthorized"),
     ErrorKind.AUTH),
    (("timestamp", "recvwindow"), ErrorKind.TIMING),
    (("mandatory parameter", "illegal characters", "not supported"),
     ErrorKind.PARAMETER),
)


class _AboveBalance(Exception):
    """Internal: a probe size exceeded the wallet balance, not the floor."""

    def __init__(self, amount: float) -> None:
        super().__init__(f"probe {amount} exceeds balance")
        self.amount = amount


class ApiError(RuntimeError):
    """
    Transient or structural API failure, carrying the venue's own code.

    The code travels with the exception so callers can branch on what went
    wrong instead of re-parsing the message.
    """

    def __init__(self, message: str, code: int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def kind(self) -> ErrorKind:
        if self.status == 404:
            return ErrorKind.NOT_FOUND      # wrong path, or unknown resource
        if self.status == 451:
            # Binance refuses restricted locations, which includes the United
            # States. A US-region host will fail every call with this.
            return ErrorKind.GEO_BLOCKED
        if self.code is not None and self.code in ERROR_CODES:
            return ERROR_CODES[self.code]
        if self.code is not None:
            return ErrorKind.UNKNOWN     # a code we do not know: do not guess
        text = str(self).lower()
        for needles, kind in _MESSAGE_HINTS:
            if any(n in text for n in needles):
                return kind
        return ErrorKind.UNKNOWN


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def _as_float_or_none(value: object) -> float | None:
    """Parse a numeric field, or None when it is absent or unusable."""
    try:
        parsed = float(value)          # float(None) raises TypeError too
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for mth in range(1, 301):
        m2 = 2 * mth
        aa = mth * (b - mth) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + mth) * (qab + mth) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b). Used for the Student-t CDF."""
    if not 0.0 <= x <= 1.0:
        raise ValueError("x must be in [0, 1]")
    if x in (0.0, 1.0):
        return x
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(x: float, df: float) -> float:
    """
    CDF of the Student-t distribution with `df` degrees of freedom.

    Implemented in stdlib to keep the runtime dependency to `requests` alone;
    verified against scipy.stats.t in the test suite.
    """
    if df <= 0:
        raise ValueError("df must be positive")
    xt = df / (df + x * x)
    tail = 0.5 * betainc(df / 2.0, 0.5, xt)
    return 1.0 - tail if x > 0 else tail


def standardised_t_cdf(z: float, df: float) -> float:
    """
    Student-t CDF rescaled to unit variance, so `z` stays comparable to a
    Gaussian z-score.

    A raw t has variance df/(df-2); without this rescaling, switching to
    fat tails would silently change the volatility as well as the shape.
    """
    if df <= 2.0:
        raise ValueError("df must exceed 2 for finite variance")
    return student_t_cdf(z * math.sqrt(df / (df - 2.0)), df)


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


def _projected_rounds(impulse: float, decay: float,
                      floor: float) -> float:
    """
    How many more rounds a decaying move stays above the noise floor.

    Geometric decay: the size after n rounds is impulse * decay**n, and the
    move stops being tradable once it drops under `floor`. Solving for n
    gives the answer in
    the units the decision is actually made in -- rounds, not ratios.

    A move that is holding or growing (decay >= 1) is not decaying at all
    and gets a large finite number rather than infinity, so callers can
    compare it without special-casing.
    """
    if impulse <= 0 or floor <= 0:
        return 0.0
    if impulse < floor:
        return 0.0                      # already under the floor
    if decay >= 1.0:
        return 99.0                     # holding or accelerating
    if decay <= 0.0:
        return 0.0                      # reversed outright
    return math.log(floor / impulse) / math.log(decay)


class OrderNotFilled(ApiError):
    """
    The venue confirmed an order did NOT fill -- killed, cancelled, rejected.

    Separate from ApiError because the two demand opposite responses. This
    one is a fact: no position exists, and recording one invents a trade that
    later "settles" and books a profit never made. A bare ApiError from the
    same call is an absence of information -- a timeout, a dropped socket --
    where a position may well exist, and dropping it strands real money that
    is never settled and never claimed. Catching both together forces one
    wrong answer or the other.
    """


class NothingToRedeem(ApiError):
    """
    The venue accepted the claim and found nothing to claim.

    Separate from ApiError for the same reason OrderNotFilled is: this one is
    a fact -- the tokens are gone, which on a winning position means the
    payout has already been credited, usually because the operator redeemed
    it by hand in the Binance app. A bare ApiError is an absence of
    information. Returning an empty hash list for both made them
    indistinguishable from a batch still in flight, so the claim worker
    re-submitted a redemption that could never succeed for the whole timeout
    and then held the token as unredeemed forever.
    """


# Message fragments that mean the same fact arrived as an error rather than
# an empty batch. Deliberately narrow: a match drops the bot's claim on real
# money, so anything vaguer than "there is nothing here to redeem" must fall
# through to the ordinary retry.
_ALREADY_REDEEMED_HINTS: tuple[str, ...] = (
    "already redeemed", "already been redeemed", "already claimed",
    "already been claimed", "no redeemable", "not redeemable",
    "nothing to redeem", "no position to redeem",
)


def _is_already_redeemed(exc: BaseException) -> bool:
    """Whether the venue's refusal says the tokens are already gone."""
    if not isinstance(exc, ApiError):
        return False        # a transport failure proves nothing either way
    text = str(exc).lower()
    return any(hint in text for hint in _ALREADY_REDEEMED_HINTS)


class VolatilityEstimator:
    """Annualised sigma AND tail thickness from recent 1m returns."""

    def __init__(self, cfg: Config | ConfigStore,
                 session: requests.Session) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._session = session
        self._cache: dict[str, tuple[float, float]] = {}
        self._df_cache: dict[str, float | None] = {}
        self._clamped: dict[str, bool] = {}
        self._raw: dict[str, float] = {}
        self._trend: dict[str, Trend] = {}

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    def sigma_annual(self, symbol: str | None = None) -> float:
        symbol = symbol or self._cfg.symbol
        cached = self._cache.get(symbol)
        if cached is not None and time.time() - cached[1] < self._cfg.vol_cache_s:
            return cached[0]

        r = self._session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m",
                    "limit": self._cfg.vol_lookback_min},
            timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
        if len(closes) < 10:
            raise ApiError("insufficient kline history for volatility")

        # Guard BOTH endpoints: a single malformed close (0 or negative) would
        # otherwise raise a math domain error and take the whole loop down.
        rets = [math.log(b / a) for a, b in itertools.pairwise(closes)
                if a > 0 and b > 0]
        if len(rets) < 10:
            raise ApiError("insufficient valid returns for volatility")
        # Recent window for sigma (volatility drifts); full window for tails.
        recent = rets[-self._cfg.sigma_window_min:]
        raw = statistics.pstdev(recent) * math.sqrt(365.0 * 24.0 * 60.0)
        annual = max(self._cfg.vol_floor_annual,
                     min(self._cfg.vol_ceiling_annual, raw))
        self._raw[symbol] = raw
        self._clamped[symbol] = abs(annual - raw) > 1e-12
        if self._clamped[symbol]:
            LOG.warning("Volatility %.4f clamped to %.4f for %s -- the model "
                        "is no longer measuring the market", raw, annual, symbol)

        self._df_cache[symbol] = self._estimate_df(rets, statistics.pstdev(rets))
        # Measured from the same closes rather than a second request: a trend
        # read off a different fetch than the sigma it is compared against is
        # two snapshots of two moments pretending to be one.
        self._trend[symbol] = self._measure_trend(closes)
        self._cache[symbol] = (annual, time.time())
        return annual

    def _measure_trend(self, closes: list[float]) -> Trend:
        """
        Direction, thrust, straightness and remaining life of the move.

        Everything comes from the closes already in hand. The series is cut
        into round-length blocks ending at NOW, so the last block is the move
        currently in progress -- that block, not the count of finished ones,
        is what says a trend is starting.

        Direction is taken from that last block too. When it disagrees with
        the blocks before it, this is a reversal and the run resets to one:
        the correct reading of a fresh reversal is "a new trend beginning",
        not "the old trend continuing", and a detector anchored to the older
        blocks would call the top of a move a buy.
        """
        cfg = self._cfg
        if not cfg.trend_follow:
            return Trend()
        window = [c for c in closes[-cfg.trend_lookback_min:] if c > 0]
        if len(window) < 10:
            return Trend()

        steps = [math.log(b / a) for a, b in itertools.pairwise(window)]
        sd_step = statistics.pstdev(steps)
        block = max(1, round(cfg.round_seconds / 60.0))
        if sd_step <= 0 or len(steps) < block:
            return Trend()

        # Blocks of one round each, oldest first, the last ending at now.
        edges = list(range(len(window) - 1, -1, -block))[::-1]
        if len(edges) < 2:
            return Trend()
        blocks = [math.log(window[b] / window[a])
                  for a, b in itertools.pairwise(edges)]

        current = blocks[-1]
        if current == 0.0:
            return Trend()
        direction = 1 if current > 0 else -1

        # One block of pure noise, as the yardstick every size is measured
        # against. Without it "a big move" would mean a fixed number of basis
        # points, which is a different thing in a calm hour than a wild one.
        sigma_block = sd_step * math.sqrt(block)
        impulse = abs(current) / sigma_block

        run = 0
        for value in reversed(blocks):
            if value == 0 or (value > 0) != (direction > 0):
                break
            run += 1

        # Straightness and strength are measured over the RUN, not over the
        # whole lookback: including blocks that moved the other way describes
        # a market that reversed, not the move being traded.
        span = min(run * block, len(window) - 1)
        segment = window[-(span + 1):]
        net = math.log(segment[-1] / segment[0])
        seg_steps = [math.log(b / a) for a, b in itertools.pairwise(segment)]
        travelled = sum(abs(s) for s in seg_steps)
        efficiency = abs(net) / travelled if travelled > 0 else 0.0
        z = (abs(net) / (sd_step * math.sqrt(len(seg_steps)))
             if seg_steps else 0.0)

        # Decay, and what it implies about how much life is left. A move
        # shedding half its size each block has nothing left for the round about
        # to start, and that is the round the entry would be taken in.
        decay = 1.0
        if run >= 2 and abs(blocks[-2]) > 0:
            decay = abs(current) / abs(blocks[-2])
        rounds_left = _projected_rounds(impulse, decay,
                                        cfg.trend_min_impulse)

        # Order matters. "Nothing is happening" and "something was happening
        # and has died" are different findings, and only the second is a
        # warning. A weak block with a run behind it is the tail of a move,
        # not the absence of one, so it is labelled fading rather than none.
        if impulse < cfg.trend_min_impulse and run <= 1:
            phase = "none"
        elif (impulse < cfg.trend_min_impulse
                or (run >= 2 and decay < cfg.trend_decay_floor)
                or rounds_left < 1.0):
            phase = "fading"
        elif run <= 1:
            phase = "building"
        else:
            phase = "running"

        return Trend(direction=direction, impulse=impulse, z=z,
                     efficiency=efficiency, run=run, decay=decay,
                     rounds_left=rounds_left, phase=phase)

    def trend(self, symbol: str | None = None) -> Trend:
        """Trend state for `symbol`. Call sigma_annual first."""
        return self._trend.get(symbol or self._cfg.symbol, Trend())

    def _estimate_df(self, rets: list[float], sd: float) -> float | None:
        """
        Degrees of freedom implied by realised excess kurtosis.

        For a Student-t, excess kurtosis = 6 / (df - 4), so df = 4 + 6/k.

        The sample estimator has standard error ~sqrt(24/n), so a fixed small
        cutoff would flag ordinary sampling noise as fat tails -- a 500-point
        Gaussian draw routinely shows +0.1 excess kurtosis. Require two
        standard errors of evidence instead, and refuse to guess at all below
        200 samples. Returns None when the data does not look convincingly
        heavy-tailed, in which case pricing stays Gaussian.
        """
        n = len(rets)
        if not self._cfg.use_fat_tails or sd <= 0 or n < 200:
            return None
        mu = statistics.fmean(rets)
        z4 = statistics.fmean([((x - mu) / sd) ** 4 for x in rets])
        excess = z4 - 3.0
        if excess <= 2.0 * math.sqrt(24.0 / n):
            return None
        df = 4.0 + 6.0 / excess
        return max(self._cfg.tail_df_floor,
                   min(self._cfg.tail_df_ceiling, df))

    def tail_df(self, symbol: str | None = None) -> float | None:
        """Tail parameter for `symbol`. Call sigma_annual first."""
        return self._df_cache.get(symbol or self._cfg.symbol)

    def raw_sigma(self, symbol: str | None = None) -> float | None:
        """Measured sigma before clamping, for diagnostics."""
        return self._raw.get(symbol or self._cfg.symbol)

    def is_clamped(self, symbol: str | None = None) -> bool:
        """True if the last sigma hit a bound and is therefore not a measurement."""
        return self._clamped.get(symbol or self._cfg.symbol, False)


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


class RiskManager:
    """Owns every reason to stop. Fails closed on all of them."""

    def __init__(self, cfg: Config | ConfigStore,
                 starting_bankroll: float) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._day_start_bankroll = max(starting_bankroll, EPS)
        self._day_key = time.strftime("%Y-%m-%d")
        self.consecutive_losses = 0
        self.rounds_today = 0
        self.halted_reason: str | None = None
        # Poisson-binomial accumulators: each trade contributes its own model
        # probability, so expectation is well defined even though every trade
        # has different odds.
        self._expected_wins = 0.0
        self._variance = 0.0
        self._actual_wins = 0
        self._samples = 0
        # Attribution. The daily loss limit used to be measured as a fall in
        # the raw venue balance, which silently counted every deposit,
        # withdrawal, transfer and manually placed order as the bot's own
        # trading result. On a small account that is not a rounding error: a
        # 5 USDT manual order against a 9 USDT balance reads as a 55%
        # drawdown and halts a bot that has not lost anything.
        #
        # So the limit is measured against realised PnL the bot can actually
        # account for, and any balance movement it cannot account for is
        # recorded as an external flow and rebased away rather than blamed on
        # the strategy.
        self._realised_pnl = 0.0
        self._external_flow = 0.0
        # The third thing a balance movement can be, and the one this used to
        # have no name for: the bot's OWN arithmetic being wrong. settle_pnl
        # models the fee and the fill, and an error in that model leaves
        # exactly the residue a deposit leaves. Every one of them was rebased
        # into the baseline and relabelled an external flow -- so a fee the
        # bot under-modelled by a few cents a round was laundered, round after
        # round, and the daily limit slowly stopped describing the account.
        # The venue is authoritative; corrections are booked as PnL and
        # counted here so the two can still be told apart in a report.
        self._pnl_correction = 0.0
        # A payout that has settled but not yet landed on chain. Recorded so
        # the drift it eventually causes can be charged to the trade rather
        # than to a phantom depositor -- and BOUNDED by it, so a withdrawal
        # sharing the same window is not swallowed whole as a trading loss.
        self._expected_credit = 0.0
        # Touched from the claim worker as well as the trading loop.
        self._lock = threading.Lock()

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @property
    def realised_pnl(self) -> float:
        """PnL from positions this bot opened and settled today."""
        return self._realised_pnl

    @property
    def external_flow(self) -> float:
        """Balance movement today that this bot did not cause."""
        return self._external_flow

    @property
    def pnl_correction(self) -> float:
        """How much the venue has moved today's PnL away from our own sums."""
        return self._pnl_correction

    def expect_credit(self, amount: float) -> None:
        """
        Declare a payout that has settled but has not landed yet.

        This is what licenses the next reconciliation to read a shortfall as
        a trading result. Without it a claim that credits light is
        indistinguishable from a withdrawal, and the bot has to assume the
        more flattering of the two.
        """
        with self._lock:
            self._expected_credit += max(amount, 0.0)

    def correct_realised_pnl(self, delta: float) -> None:
        """
        Book a difference the venue reported against what we calculated.

        Called where the measurement is trustworthy on its own -- either side
        of a single settlement, seconds apart, far too narrow a window for a
        deposit to be a plausible explanation.
        """
        if abs(delta) <= EPS:
            return
        with self._lock:
            self._realised_pnl += delta
            self._pnl_correction += delta

    def calibration_z(self) -> float | None:
        """
        How far observed wins sit below what the model predicted, in sigmas.

        None until there are enough samples. This is the honest health check
        for a low-win-rate strategy: losing 12 in a row on 10%-probability
        contracts is expected, whereas losing 12 in a row on 60% contracts
        means the model is broken. A streak counter cannot tell them apart.
        """
        if self._samples < self._cfg.calibration_min_samples:
            return None
        if self._variance <= EPS:
            return None
        return (self._actual_wins - self._expected_wins) / math.sqrt(self._variance)

    def _roll_day(self, bankroll: float) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day_key:
            self._day_key = today
            self._day_start_bankroll = max(bankroll, EPS)
            self.consecutive_losses = 0
            self.rounds_today = 0
            self.halted_reason = None
            self._expected_wins = self._variance = 0.0
            self._actual_wins = self._samples = 0
            self._realised_pnl = 0.0
            self._external_flow = 0.0
            self._pnl_correction = 0.0
            self._expected_credit = 0.0
            LOG.info("New trading day; baseline bankroll %.2f", bankroll)

    def reconcile(self, bankroll: float,
                  outstanding: float | None = None) -> float:
        """
        Separate what this bot did to the balance from what anything else did.

        With nothing in flight the balance is fully predictable: the day's
        opening figure plus everything the bot has settled. Whatever is left
        over came from somewhere else -- a deposit, a withdrawal, a transfer
        between wallets, or an order placed by hand -- and is none of the
        strategy's doing. That residue is folded into the day's baseline
        instead of being counted as a result, and returned so the caller can
        report it.

        WHY THIS ONLY RUNS WHEN FLAT
        ----------------------------
        `bankroll` and the expectation above are measured on different bases
        the moment anything is outstanding, and the difference is the bot's
        OWN money -- which is exactly what this must not mistake for someone
        else's:

          * an open position. A wallet-funded account reads its balance from
            the portfolio's totalCurrentValue, which marks open positions to
            MARKET, while the arithmetic here knows them at COST. Every tick
            of the underlying then looked like a deposit or a withdrawal: a
            4.00 leg bought at 0.25 rebased the baseline by +4.00 on the spot
            and by another fraction of a USDT on every poll afterwards, with
            no trade taking place at all. Paper mode had the same symptom for
            a different reason -- _paper_bankroll is not debited when a stake
            goes out, so an open position read as a deposit of its own size.

          * a claim in flight. A win is booked into realised PnL the instant
            it settles, but the USDT lands on chain up to claim_timeout_s
            later. In between, the balance is short by the payout and the
            gap read as a withdrawal; when the redemption confirmed, the same
            payout arrived and read as a deposit. Every won round logged two
            phantom movements that cancelled out only by luck.

        So reconciliation waits for a flat book. Nothing is lost by waiting:
        drift is measured against the day's baseline plus cumulative realised
        PnL, so a genuine deposit made mid-round is still caught in full the
        first moment the bot is flat. `outstanding` is None for callers that
        do not own the balance at all -- the per-market managers share one
        account and must never rebase it.

        The tolerance is derived from the venue minimum rather than being a
        new tunable: anything smaller than a fraction of the smallest order
        the venue accepts cannot be a trade.

        WHAT A CLAIM IN FLIGHT CHANGES
        ------------------------------
        Not every residue is somebody else's money. A payout that settled and
        has not landed yet is the bot's own, and when it lands SHORT -- a fee
        the model missed, a partial redemption -- the difference is a trading
        result. Charging it to a phantom depositor is how a wrong fee model
        stays invisible: rebased away once per won round, for as long as the
        bot runs, while the daily limit drifts further from the account it is
        supposed to be protecting.

        So a declared credit (expect_credit) licenses a correction, and
        reconcile_tolerance bounds it: the venue may differ from our sums by
        that fraction of the payout, which is what an unmodelled fee or a
        partial redemption looks like. That is the same meaning the knob
        already carries at settlement, applied to the same question.

        The bound is the point. Drift beyond what a fee could explain is not
        a fee, so a withdrawal landing in the same window as a claim still
        reads as a withdrawal for all but a sliver -- which is what keeps a
        manual order from reading as a drawdown. The split of a genuinely
        ambiguous residue cannot be recovered; it can only be capped, and
        capping it in the strategy's DISFAVOUR is the safe direction.
        """
        if outstanding is None:
            return 0.0
        tolerance = max(0.01, self._cfg.min_stake_usdt * 0.10)
        if outstanding > tolerance:
            LOG.debug("Not reconciling: %.2f USDT still in flight (staked, "
                      "or won and not yet credited)", outstanding)
            return 0.0
        expected = self._day_start_bankroll + self._realised_pnl
        drift = bankroll - expected
        with self._lock:
            claimable = self._expected_credit
            # Flat, so anything owed has either landed or is not coming. The
            # expectation explains this reconciliation and no later one.
            self._expected_credit = 0.0
        if abs(drift) <= tolerance:
            return 0.0

        room = claimable * self._cfg.reconcile_tolerance
        correction = max(-room, min(room, drift))
        if abs(correction) > EPS:
            self._realised_pnl += correction
            self._pnl_correction += correction
            LOG.info("Venue credited %+.2f USDT against what this bot "
                     "calculated; booked as a trading result, not an external "
                     "movement. Today's corrections %+.2f USDT.",
                     correction, self._pnl_correction)
            drift -= correction
            if abs(drift) <= tolerance:
                return 0.0

        self._day_start_bankroll = max(self._day_start_bankroll + drift, EPS)
        self._external_flow += drift
        LOG.info("External balance movement %+.2f USDT (deposit, withdrawal "
                 "or an order this bot did not place); baseline rebased to "
                 "%.2f. Not counted as a trading result.",
                 drift, self._day_start_bankroll)
        return drift

    def check(self, bankroll: float,
              outstanding: float | None = None) -> None:
        self._roll_day(bankroll)
        self.reconcile(bankroll, outstanding)
        if self.halted_reason:
            raise TradingHalted(self.halted_reason)

        # Measured on the bot's OWN realised PnL, not on the balance. The two
        # differ by every external flow, and only one of them is the
        # strategy's performance.
        drawdown = -self.realised_pnl / self._day_start_bankroll
        if drawdown >= self._cfg.daily_loss_limit_pct:
            # Report both figures. "Down 35%" invites the question the old
            # message could not answer -- down from what, and did the bot do
            # it? -- and answering it in the halt line is the difference
            # between a diagnosis and a mystery.
            self._halt(f"daily loss limit: {drawdown:.1%} down on this bot's "
                       f"own trades ({self.realised_pnl:+.2f} USDT, including "
                       f"{self.pnl_correction:+.2f} of venue correction; "
                       f"external movements {self.external_flow:+.2f} USDT "
                       f"excluded) "
                       f"(limit {self._cfg.daily_loss_limit_pct:.0%})")
        z = self.calibration_z()
        if z is not None:
            # Enough data for the statistically correct test, so use it and
            # ignore the streak counter entirely. A convex strategy buying
            # 12%-probability contracts hits 30-loss streaks several times per
            # 200 trades; halting on that would shut down a healthy bot.
            if z <= self._cfg.calibration_z_halt:
                self._halt(f"results {abs(z):.1f} sigma below model prediction "
                           f"over {self._samples} trades -- the model is "
                           f"overconfident, not merely unlucky")
        elif self.consecutive_losses >= self._cfg.max_consecutive_losses:
            # Fallback only while the sample is too small to judge properly.
            self._halt(f"{self.consecutive_losses} consecutive losses before "
                       f"enough data to assess calibration; manual review")
        if self.rounds_today >= self._cfg.max_rounds_per_day:
            self._halt("max rounds per day reached")
        if bankroll < self._cfg.min_stake_usdt:
            self._halt("bankroll below minimum stake")

    def _halt(self, reason: str) -> None:
        self.halted_reason = reason
        raise TradingHalted(reason)

    def record_result(self, won: bool,
                      model_prob: float | None = None,
                      pnl: float | None = None) -> None:
        """
        Book a result THIS bot produced.

        pnl is what makes the daily limit meaningful: without it the manager
        knows a trade happened but not what it cost, and has to fall back on
        reading the balance -- which is exactly the conflation this avoids.
        """
        self.rounds_today += 1
        self.consecutive_losses = 0 if won else self.consecutive_losses + 1
        if pnl is not None:
            self._realised_pnl += pnl
        if model_prob is not None:
            self._expected_wins += model_prob
            self._variance += model_prob * (1.0 - model_prob)
            self._actual_wins += int(won)
            self._samples += 1


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class PredictionClient:
    """All venue I/O. Signs requests; parses payloads exactly once."""

    # Set from Config at construction; _parse_round is a staticmethod and
    # needs the target duration without a config reference.
    round_target_ms: int = DEFAULT_ROUND_SECONDS * 1000
    open_statuses: tuple[str, ...] = ("REGISTERED", "OPEN", "ACTIVE")
    tradable_status: str = "OPEN"
    duration_tolerance: float = 0.10
    # Empty means no restriction -- see Config.symbols.
    symbols: tuple[str, ...] = ()

    def __init__(self, cfg: Config | ConfigStore) -> None:
        # Accepts either a Config or a ConfigStore. With a store, `_cfg`
        # resolves to the current configuration on every access, so a hot
        # reload takes effect without rebuilding the client or its session.
        self._store: ConfigStore | None = None
        self._static_cfg: Config | None = None
        if isinstance(cfg, Config):
            self._static_cfg = cfg
        else:
            self._store = cfg
        cfg = self._cfg
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": cfg.api_key})
        self._clock_offset_ms = 0
        self._wallet: WalletRef | None = None
        self._symbol_cache: dict[str, str] = {}
        self.apply_config(cfg)
    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @staticmethod
    def apply_config(cfg: Config) -> None:
        """
        Push settings that _parse_round reads as class attributes.

        _parse_round is a staticmethod (it validates untrusted payloads with
        no instance to hand), so these must be refreshed whenever the config
        changes -- otherwise a hot reload would update everything except
        parsing, and the two would silently disagree.
        """
        PredictionClient.round_target_ms = cfg.round_seconds * 1000
        PredictionClient.open_statuses = cfg.open_statuses
        PredictionClient.tradable_status = cfg.tradable_status
        PredictionClient.duration_tolerance = cfg.round_duration_tolerance
        PredictionClient.symbols = tuple(cfg.symbols)

    @property
    def session(self) -> requests.Session:
        return self._session

    # -- time ---------------------------------------------------------------

    def sync_clock(self) -> int:
        r = self._session.get(BASE + "/api/v3/time",
                              timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        self._clock_offset_ms = (int(r.json()["serverTime"])
                                 - int(time.time() * 1000))
        if abs(self._clock_offset_ms) > 1000:
            LOG.warning("Local clock off by %d ms; compensating",
                        self._clock_offset_ms)
        return self._clock_offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    # -- transport ----------------------------------------------------------

    def _signed_query(self, params: dict) -> str:
        """
        Build the exact query string to send, with its signature appended.

        Binance recomputes the HMAC over the query string it RECEIVES, so the
        signed bytes and the sent bytes must be byte-identical. Signing a
        sorted dict and then letting the HTTP client re-serialise it in
        insertion order produces a different string and a guaranteed -1022
        signature error. Returning a string rather than a dict makes that
        class of bug unrepresentable.

        doseq=True is required for repeated parameters such as tokenIds;
        without it a list serialises as its Python repr.
        """
        p = {k: v for k, v in params.items() if v is not None}
        p["timestamp"] = self.now_ms()
        p["recvWindow"] = self._cfg.recv_window_ms
        query = urllib.parse.urlencode(sorted(p.items()), doseq=True)
        signature = hmac.new(self._cfg.api_secret.encode(),
                             query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"
    
    # Binance error codes worth explaining rather than echoing verbatim.
    _ERROR_HINTS = {  # noqa: RUF012
        -1022: "signature mismatch -- the signed and sent query strings differ",
        -1021: "timestamp outside recvWindow -- clock drift",
        -1102: "a mandatory parameter was missing or malformed",
        -2014: "API-key format invalid",
        -2015: "invalid API key, IP not whitelisted, or missing permissions",
        -1002: "not authorised for this endpoint",
        -9000: "the account balance cannot cover this order size",
        -3026: ("a parameter combination the venue rejected -- most often "
                "fundingSource not matching accountType (SPOT/FUNDING are "
                "CEX accounts, not MPC), or a missing fundTransferAmount"),
    }

    @staticmethod
    def _json_or_none(response) -> object | None:
        """Parsed JSON, or None when the body is not JSON at all."""
        try:
            return response.json()
        except ValueError:
            return None

    def _request(self, name: str, params: dict | None = None) -> dict:
        """Call a named endpoint. The verb comes from the table, never a caller."""
        method, path = self._cfg.ep(name)
        query = self._signed_query(params or {})
        if LOG.isEnabledFor(logging.DEBUG):
            # Signature redacted; every other parameter shown verbatim so a
            # malformed request is visible rather than inferred.
            LOG.debug("%s %s?%s", method, path,
                      re.sub(r"signature=[0-9a-f]+", "signature=<redacted>",
                             query))
        url = f"{BASE}{path}?{query}"
        try:
            r = self._session.request(method, url,
                                      timeout=self._cfg.http_timeout_s)
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path}: {exc}") from exc

        if r.status_code >= 400:
            # Binance puts the real diagnosis in the body, not the status line.
            # Discarding it turns every distinct failure into "400 Bad Request".
            code, body = None, self._json_or_none(r)
            if isinstance(body, dict) and "msg" in body:
                code = body.get("code")
                detail = f"{body['msg']} (code {code})"
            else:
                # Not a JSON error envelope; the raw text is the best detail
                # available and is preserved rather than discarded.
                detail = r.text[:200] or "<empty response body>"
            code_int: int | None = None
            if code is not None:
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = None
            hint = self._ERROR_HINTS.get(code_int)
            raise ApiError(f"{method} {path}: HTTP {r.status_code}: {detail}"
                           + (f" -- {hint}" if hint else ""),
                           code=code_int, status=r.status_code)

        try:
            payload = r.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path}: bad JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"{method} {path}: expected a JSON object")
        return payload

    # -- public spot (model input only) -------------------------------------

    def market_symbol(self, feed_symbol: str | None) -> str:
        """
        Map the venue's oracle symbol to a tradable Binance symbol.

        The market resolves on its own feed (e.g. Pyth "BTC/USD"), which is not
        a valid Binance ticker. Passing it through unchecked makes every price
        request fail. Normalise, verify once, and fall back to BTCUSDT with a
        loud warning -- the fallback carries basis risk, so it must be visible
        rather than silent.
        """
        if not feed_symbol:
            return self._cfg.symbol
        if feed_symbol in self._symbol_cache:
            return self._symbol_cache[feed_symbol]

        candidate = re.sub(r"[^A-Z0-9]", "", feed_symbol.upper())
        resolved = self._cfg.symbol
        if candidate:
            try:
                r = self._session.get(BASE + "/api/v3/ticker/price",
                                      params={"symbol": candidate},
                                      timeout=self._cfg.http_timeout_s)
                if r.status_code == 200:
                    resolved = candidate
            except requests.RequestException as exc:
                LOG.warning("Could not verify symbol %r (%s); falling back",
                            candidate, exc)

        if resolved != candidate:
            LOG.warning("Settlement feed %r is not a Binance symbol; modelling "
                        "on %s instead. Basis risk between the two feeds "
                        "is NOT captured by the model.", feed_symbol,
                        self._cfg.symbol)
        self._symbol_cache[feed_symbol] = resolved
        return resolved

    def spot_price(self, symbol: str | None = None) -> float:
        r = self._session.get(BASE + "/api/v3/ticker/price",
                              params={"symbol": symbol or self._cfg.symbol},
                              timeout=self._cfg.http_timeout_s)
        if r.status_code == 451:
            raise ApiError(
                "HTTP 451: Binance blocks this server's region. Host outside "
                "the United States (Frankfurt or Singapore on Render).",
                status=451)
        r.raise_for_status()
        return float(r.json()["price"])

    # -- account ------------------------------------------------------------

    def wallet(self) -> WalletRef:
        """Prediction wallet address + id. Cached; required by most calls."""
        if self._wallet is not None:
            return self._wallet
        payload = self._request("wallet_list")
        for w in payload.get("wallets") or []:
            addr, wid = w.get("walletAddress"), w.get("walletId")
            if addr and wid:
                self._wallet = WalletRef(str(addr), str(wid))
                return self._wallet
        raise ApiError("no prediction wallet found -- create one in the "
                       "Binance app and complete SAS authorization")

    def prediction_wallet_value(self) -> float | None:
        """
        Current value held inside the prediction wallet, from the portfolio.

        payment-options reports the CEX accounts; it does not necessarily
        include the prediction wallet, so a funded prediction account can read
        as 0.00 there. This is the second place to look.
        """
        try:
            payload = self._request("portfolio",
                                    {"walletAddress": self.wallet().address})
        except ApiError as exc:
            LOG.debug("Portfolio lookup failed: %s", exc)
            return None
        raw = payload.get("totalCurrentValue")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable totalCurrentValue %r", raw)
            return None
        return value if math.isfinite(value) else None

    def payment_options(self) -> list[tuple[str, float, bool]]:
        """
        Every funding option as (accountType, balance, enabled).

        Returned whole rather than filtered, because collateral can sit under
        an account type the caller did not anticipate. Silently filtering to
        one type reports 0.00 for a funded account -- which looks like an
        empty wallet rather than a lookup in the wrong place.
        """
        payload = self._request("balances")
        out: list[tuple[str, float, bool]] = []
        for item in payload.get("items") or []:
            try:
                bal = float(item.get("availableBalanceDisplay") or 0.0)
            except (TypeError, ValueError):
                bal = 0.0
            out.append((str(item.get("accountType") or "UNKNOWN").upper(),
                        bal, bool(item.get("enabled", True))))
        return out

    def funding_plan(self) -> tuple[str, str, str | None]:
        """
        Decide how an order gets paid for: (accountType, fundingSource, holder).

        Three places can hold collateral, and they are not interchangeable:

          * the prediction wallet itself (the MPC wallet) -- funds are already
            where the order needs them, so fundingSource=MPC and no transfer;
          * SPOT or FUNDING -- Binance exchange accounts, so fundingSource=CEX
            and the collateral must be moved in.

        `accountType` on place-order accepts ONLY SPOT or FUNDING. The
        prediction wallet is not a legal value there, so when it holds the
        funds we still nominate a CEX account for the parameter and let
        fundingSource=MPC say where the money really is. Passing the
        prediction account through verbatim is what produced -3026.

        `holder` is the account type actually holding the largest balance, or
        None when nothing is funded.
        """
        options = [(t, b) for t, b, en in self.payment_options() if en]
        holder = max(options, key=lambda kv: kv[1])[0] if options else None

        if self._cfg.funding_source != "AUTO":
            funding = self._cfg.funding_source
        elif holder is None or holder not in CEX_ACCOUNT_TYPES:
            funding = "MPC"        # already in the prediction wallet
        else:
            funding = "CEX"

        if self._cfg.account_type != "AUTO":
            account = self._cfg.account_type
        elif holder in CEX_ACCOUNT_TYPES:
            account = holder
        else:
            # Nominate a valid CEX account; fundingSource carries the truth.
            cex = [(t, b) for t, b in options if t in CEX_ACCOUNT_TYPES]
            account = max(cex, key=lambda kv: kv[1])[0] if cex else "SPOT"

        if account not in CEX_ACCOUNT_TYPES:
            raise ApiError(f"accountType {account!r} is not a valid payment "
                           f"account; must be one of {CEX_ACCOUNT_TYPES}")
        return account, funding, holder

    def resolved_funding_source(self) -> str:
        return self.funding_plan()[1]

    def balance_usdt(self) -> float:
        """
        Collateral available to trade, across every place it can sit.

        payment-options covers the CEX accounts only. A prediction wallet
        funded directly does not appear there, so reading that endpoint alone
        reports 0.00 for an account that plainly has money in it. Both sources
        are consulted and the larger is used.
        """
        options = self.payment_options()
        usable = [(t, b) for t, b, en in options if en]

        if self._cfg.account_type != "AUTO":
            for acct, bal in usable:
                if acct == self._cfg.account_type:
                    return bal
            raise ApiError(
                f"no enabled {self._cfg.account_type} option; available: "
                + ", ".join(f"{t}={b:.2f}" for t, b in usable) or "none")

        best = max((b for _, b in usable), default=0.0)
        in_wallet = self.prediction_wallet_value()
        if in_wallet is not None and in_wallet > best:
            LOG.debug("Using prediction wallet balance %.2f USDT", in_wallet)
            return in_wallet
        if not usable and in_wallet is None:
            raise ApiError("no funded account found: payment-options is empty "
                           "and the portfolio could not be read")
        return best

    def remaining_quota_usdt(self) -> float | None:
        """
        Venue-imposed daily trading limit, or None if the venue reports none.

        Errors propagate: swallowing them here made preflight print "OK None"
        for an endpoint that had actually failed, which is worse than no check.
        """
        payload = self._request("quota_status")
        raw = payload.get("remainingDailyLimit")
        return None if raw is None else float(raw)

    # -- market data --------------------------------------------------------

    def list_rounds(self) -> list[Round]:
        payload = self._request("market_list", {
            "l1Category": "crypto", "l2Category": "up-down",
            "sortBy": "END_DATE", "orderBy": "ASC",
            "limit": self._cfg.market_list_limit})
        # One listing call covers every up/down market; _parse_round keeps
        # only the configured symbols, or every symbol when none are
        # configured -- that is how auto-discovery of new markets works.
        out = []
        for topic in payload.get("marketTopics") or []:
            rnd = self._parse_round(topic)
            if rnd is not None:
                out.append(rnd)
        return out

    def market_detail(self, topic_id: int) -> dict:
        return self._request("market_detail",
                             {"marketTopicId": topic_id})

    def hydrate(self, rnd: Round) -> Round | None:
        """
        Fill in strike and feed symbol from market/detail.

        The strike is variantData.startPrice. Reconstructing it from Binance
        klines would introduce basis risk, because the market resolves on its
        own price feed rather than on Binance spot.
        """
        if rnd.strike is not None:
            return rnd
        try:
            topic = self.market_detail(rnd.topic_id)
        except ApiError as exc:
            LOG.warning("Market detail unavailable for %s: %s", rnd.slug, exc)
            return None
        vd = (topic.get("marketTopic") or topic).get("variantData") or {}
        strike, symbol = self._parse_variant(vd)
        return None if strike is None else replace(rnd, strike=strike,
                                                   feed_symbol=symbol)

    @staticmethod
    def _parse_variant(vd: dict) -> tuple[float | None, str | None]:
        """(startPrice, priceFeedSymbol) from a variantData block."""
        if not isinstance(vd, dict):
            return None, None
        symbol = vd.get("priceFeedSymbol")
        raw = vd.get("startPrice")
        if raw is None:
            return None, symbol         # not published yet: expected early
        try:
            price = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable startPrice %r", raw)
            return None, symbol
        if not math.isfinite(price):
            LOG.warning("Non-finite startPrice %r", raw)
            return None, symbol
        if price <= 0:
            LOG.warning("Implausible startPrice %r", raw)
            return None, symbol
        return price, symbol

    @staticmethod
    def _parse_round(topic: dict) -> Round | None:
        """
        Validate untrusted payload once, into a precise type.

        Returns None for anything that is not a live 5-minute up/down market
        in a configured symbol (every symbol, when none are configured).
        Downstream code may assume every Round is well-formed. `strike` is left
        None here: the list response often omits variantData, and requiring it
        would reject every round and leave the bot silently never trading.
        """
        try:
            if topic.get("chartType") != "CRYPTO_UP_DOWN":
                return None
            # Empty PredictionClient.symbols means no restriction: every
            # symbol the venue lists is a candidate market.
            if (PredictionClient.symbols
                    and topic.get("symbol") not in PredictionClient.symbols):
                return None
            if topic.get("status") not in PredictionClient.open_statuses:
                return None

            start_ms, end_ms = int(topic["startDate"]), int(topic["endDate"])
            target = PredictionClient.round_target_ms
            if abs((end_ms - start_ms) - target) > target * PredictionClient.duration_tolerance:
                return None

            markets = topic.get("markets") or []
            if not markets:
                return None
            market = markets[0]
            if market.get("tradingStatus") != PredictionClient.tradable_status:
                return None

            outcomes = {str(o.get("name", "")).upper(): o
                        for o in market.get("outcomes") or []}
            up = outcomes.get("YES") or outcomes.get("UP")
            down = outcomes.get("NO") or outcomes.get("DOWN")
            if not up or not down:
                return None

            up_q, down_q = float(up["price"]), float(down["price"])
            if not (math.isfinite(up_q) and math.isfinite(down_q)):
                return None
            if not (0.0 < up_q < 1.0 and 0.0 < down_q < 1.0):
                return None

            vendor = topic.get("vendor")
            chain_id = topic.get("chainId")
            collateral = topic.get("collateral")
            if not vendor or not chain_id or not collateral:
                # Required for order routing. Guessing them would send a
                # correctly-formed order to the wrong place.
                LOG.warning("Market %s missing vendor/chainId/collateral",
                            topic.get("slug"))
                return None

            # Distinct names on purpose: `market_symbol` is the venue's ticker
            # for the contract (BTCUSDT), while `feed_symbol` is the oracle it
            # settles against. Sharing one name built the Round with the
            # ORACLE symbol -- or the string "None" when no feed was published
            # -- so positions, per-market risk and the journal were all keyed
            # on the wrong value.
            market_symbol = str(topic.get("symbol") or "")
            if not market_symbol:
                LOG.warning("Market topic has no symbol; skipping")
                return None
            strike, feed_symbol = PredictionClient._parse_variant(
                topic.get("variantData") or {})

            fee_raw = topic.get("feeRateBps")
            if fee_raw is None:
                LOG.warning("Market %s publishes no feeRateBps", topic.get("slug"))
                return None
            fee_bps = int(fee_raw)
            if not 0 <= fee_bps < 10_000:
                # A negative rate would inflate net odds and therefore stake
                # size; a rate at or above 100% makes the contract unpayable.
                # Neither is a market we should trade.
                LOG.warning("Market %s publishes implausible feeRateBps %s",
                            topic.get("slug"), fee_raw)
                return None

            prec_raw = market.get("decimalPrecision")
            if prec_raw is None:
                # Defaulting to 4 would round prices to a precision the venue
                # does not use, producing orders it may reject.
                LOG.warning("Market %s publishes no decimalPrecision",
                            topic.get("slug"))
                return None

            liq_raw = market.get("liquidity")
            if liq_raw is None:
                liq_raw = topic.get("liquidity")
            try:
                liquidity = None if liq_raw is None else float(liq_raw)
            except (TypeError, ValueError, OverflowError):
                liquidity = None
            if liquidity is not None and not math.isfinite(liquidity):
                liquidity = None

            return Round(
                topic_id=int(topic["marketTopicId"]),
                market_id=int(market["marketId"]),
                vendor=str(vendor),
                slug=str(topic.get("slug", "")),
                symbol=market_symbol,
                start_ms=start_ms, end_ms=end_ms,
                up_token_id=str(up["tokenId"]),
                down_token_id=str(down["tokenId"]),
                up_quote=up_q, down_quote=down_q,
                fee_bps=fee_bps,
                chain_id=str(chain_id),
                collateral=str(collateral).upper(),
                venue_slippage_bps=int(topic.get("slippageBps") or 0),
                decimal_precision=int(prec_raw),
                liquidity=liquidity,
                strike=strike, feed_symbol=feed_symbol)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # OverflowError is NOT a ValueError: int(float("inf")) raises it,
            # and "1e999" parses to inf before reaching int().
            LOG.warning("Skipping malformed market payload: %s", exc)
            return None

    def asks_for(self, rnd: Round, side: Side
                 ) -> list[tuple[float, float]] | None:
        """Ask ladder for one outcome. `vendor` is a required parameter."""
        try:
            payload = self._request("order_book", {
                "vendor": rnd.vendor, "marketId": rnd.market_id,
                "tokenId": rnd.token_for(side)})
        except ApiError as exc:
            LOG.debug("order book unavailable: %s", exc)
            return None
        return self._parse_asks(payload)

    @staticmethod
    def _parse_asks(payload: dict) -> list[tuple[float, float]] | None:
        """Levels are {price, size} strings per the connector schema."""
        raw = payload.get("asks")
        if raw is None:
            nested = payload.get("orderBook") or payload.get("data") or {}
            raw = nested.get("asks") if isinstance(nested, dict) else None
        if not raw:
            return None
        if not isinstance(raw, (list, tuple)):
            # A scalar here is malformed; iterating it raises TypeError.
            LOG.warning("Order book 'asks' is %s, not a list",
                        type(raw).__name__)
            return None

        levels: list[tuple[float, float]] = []
        skipped = 0
        for lvl in raw:
            try:
                if isinstance(lvl, dict):
                    price = float(lvl["price"])
                    size_raw = lvl.get("size")
                    if size_raw is None:
                        size_raw = lvl.get("quantity")
                    if size_raw is None:
                        size_raw = lvl.get("amount")
                    size = float(size_raw)
                else:
                    price, size = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, KeyError, IndexError,
                    OverflowError):
                skipped += 1
                continue
            if not (math.isfinite(price) and math.isfinite(size)):
                skipped += 1
                continue
            if 0.0 < price < 1.0 and size > 0:
                levels.append((price, size))
            else:
                skipped += 1
        if skipped:
            # Silently dropping levels would understate depth and make the
            # book look thinner than it is.
            LOG.warning("Order book: skipped %d unparseable level(s) of %d",
                        skipped, len(raw))
        return sorted(levels) or None

    # -- trading ------------------------------------------------------------

    def effective_slippage_bps(self, rnd: Round) -> int:
        """
        The tighter of our risk cap and the venue's published tolerance.

        The venue's value is a maximum it will accept, not a recommendation --
        the listed default of 1200 bps would let a thin book fill us 12% worse
        than quoted. Taking the minimum keeps our cap authoritative without
        inventing a number the venue would reject.
        """
        venue = rnd.venue_slippage_bps
        if venue <= 0:
            return self._cfg.max_slippage_bps
        return max(1, min(self._cfg.max_slippage_bps, venue))

    def get_quote(self, rnd: Round, side: Side, stake_usdt: float) -> Quote:
        """
        Phase 1 of trading: ask the venue to price the trade.

        Returns the authoritative average fill price, so no local book-walking
        estimate is needed once we are live.
        """
        payload = self._request("get_quote", {
            "walletAddress": self.wallet().address,
            "tokenId": rnd.token_for(side),
            "side": "BUY",
            "amountIn": to_wei(stake_usdt),
            "orderType": "MARKET",
            "slippageBps": self.effective_slippage_bps(rnd),
            "chainId": rnd.chain_id,
            "feeRateBps": rnd.fee_bps,
            "fundingSource": self.resolved_funding_source()})

        quote_id = payload.get("quoteId")
        avg = payload.get("averagePrice")
        if not quote_id or avg is None:
            raise ApiError(f"malformed quote response: {payload}")

        avg_f = float(avg)
        if not 0.0 < avg_f < 1.0:
            raise ApiError(f"quote returned implausible price {avg_f}")

        shares_raw = payload.get("amountOut")
        if shares_raw is None:
            raise ApiError(f"quote omits amountOut: {payload}")
        shares = float(from_wei(shares_raw))
        if shares <= 0:
            raise ApiError(f"quote returned {shares} shares for "
                           f"{stake_usdt:.2f} USDT")

        # Cross-check: shares x average price should reconcile with the
        # amount spent. A mismatch means averagePrice and amountOut describe
        # different things, and every downstream calculation would be wrong.
        implied = shares * avg_f
        tol = self._cfg.quote_consistency_tolerance
        if implied > 0 and abs(implied - stake_usdt) / stake_usdt > tol:
            raise ApiError(
                f"quote is internally inconsistent: {shares:.4f} shares at "
                f"{avg_f:.4f} implies {implied:.2f} USDT, not {stake_usdt:.2f}")

        impact_raw = payload.get("priceImpact")
        fee_raw = payload.get("feeAmount")
        return Quote(
            quote_id=str(quote_id),
            average_price=avg_f,
            amount_out_shares=shares,
            # A missing impact is unknown, not zero; treat it as the worst
            # case so the caller's impact guard cannot be bypassed.
            price_impact=(float("inf") if impact_raw is None
                          else float(impact_raw)),
            fee_usdt=0.0 if fee_raw is None else float(from_wei(fee_raw)))

    def discover_min_stake(self, rnd: Round, side: Side,
                           low: float = 0.25, high: float | None = None,
                           tolerance: float = 0.05) -> float | None:
        """
        Find the venue's actual minimum order size by probing get-quote.

        The connector documents "approximately 1.5 USDT (varies by market
        depth)" and publishes no field for it, so it cannot be read -- but it
        can be measured. Quotes are non-binding and place no order.

        `high` defaults to the account's own balance rather than an invented
        ceiling: probing above what the wallet holds returns "not enough
        USDT", which says nothing about the minimum and previously crashed
        the search. Errors are classified by the venue's numeric code, so a
        funds problem, an auth problem and a genuine size floor are never
        confused with one another.

        Returns None when no tested size quotes successfully.
        """
        if high is None:
            try:
                high = max(low * 2.0, self.balance_usdt())
            except ApiError as exc:
                LOG.warning("Could not read balance to bound the search: %s",
                            exc)
                high = low * 4.0
        if low <= 0 or high <= low:
            raise ValueError("require 0 < low < high")

        def quotable(amount: float) -> bool:
            """
            True if the venue quotes this size.

            Only a SIZE rejection counts as "too small". A funds error means
            the probe exceeded the balance and says nothing about the floor,
            so it bounds the search downward instead. Anything else is
            re-raised: treating an auth or parameter failure as "amount below
            minimum" turns a broken request into a confident wrong conclusion.
            """
            try:
                self.get_quote(rnd, side, amount)
                return True
            except ApiError as exc:
                if exc.kind is ErrorKind.SIZE:
                    LOG.debug("size %.2f rejected as too small: %s", amount, exc)
                    return False
                if exc.kind is ErrorKind.INSUFFICIENT_FUNDS:
                    LOG.debug("size %.2f exceeds the balance, not the floor",
                              amount)
                    raise _AboveBalance(amount) from exc
                raise                       # not a size problem -- surface it

        lo, hi = low, high
        try:
            if quotable(low):
                return low
            if not quotable(hi):
                return None
            while hi - lo > tolerance:
                mid = (lo + hi) / 2.0
                if quotable(mid):
                    hi = mid
                else:
                    lo = mid
            return hi
        except _AboveBalance as exc:
            # The probe ran past the wallet balance. Retry below it rather
            # than discarding everything learned so far.
            ceiling = exc.amount * 0.9
            if ceiling <= lo + tolerance:
                return None
            LOG.info("Re-bounding the search below the balance (%.2f)",
                     ceiling)
            return self.discover_min_stake(rnd, side, low, ceiling, tolerance)

    def place_order(self, rnd: Round, quote: Quote,
                    stake_usdt: float | None = None) -> str:
        """
        Phase 2: execute a quote. MARKET orders are FOK -- fill-or-kill, so
        there are no partial fills at prices the model never approved.

        Takes the round so chain and slippage come from the market rather
        than from a module-level assumption.

        Returns the venue order id. The response carries no fill price; the
        quote's averagePrice is the executed price.
        """
        wallet = self.wallet()
        account, funding, holder = self.funding_plan()
        params = {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "quoteId": quote.quote_id,
            "timeInForce": "FOK",          # required pairing for MARKET
            "accountType": account,
            "orderType": "MARKET",
            "slippageBps": self.effective_slippage_bps(rnd),
            "fundingSource": funding,
        }
        LOG.debug("Funding plan: accountType=%s fundingSource=%s holder=%s",
                  account, funding, holder)
        # A CEX-funded order needs the collateral moved to the prediction
        # wallet first; fundTransferAmount asks the venue to do it inline.
        if funding == "CEX" and self._cfg.auto_fund_transfer \
                and stake_usdt is not None:
            params["fundTransferAmount"] = to_wei(stake_usdt)

        payload = self._request("place_order", params)

        order_id = payload.get("orderId")
        if not order_id:
            raise ApiError(f"order not accepted: {payload}")
        return str(order_id)

    # Statuses that mean the order is done and did NOT result in a position.
    DEAD_ORDER_STATUSES = frozenset({
        "CANCELLED", "CANCELED", "KILLED", "EXPIRED", "REJECTED", "FAILED",
        "TERMINATED",
    })
    FILLED_ORDER_STATUSES = frozenset({"FILLED", "COMPLETED", "SUCCESS"})

    def order_fill(self, order_id: str) -> dict | None:
        """
        The venue's own record of an order: status, filled amount, price.

        Returns None when the order is not in the history yet, which is
        different from "it did not fill" and must not be conflated with it.
        """
        payload = self._request("order_history", {
            "walletAddress": self.wallet().address,
            "l1Category": "crypto",
            "limit": self._cfg.settled_history_limit})
        for order in payload.get("orders") or []:
            if str(order.get("orderId")) == str(order_id):
                return order
        return None

    def confirm_fill(self, order_id: str, requested_usdt: float) -> float:
        """
        Confirm an order filled, returning the USDT actually filled.

        Polls because the history may lag the placement by a moment. Raises
        rather than returning zero when the order is dead or the fill is too
        small: a caller that receives a number can carry on with a position
        that does not exist, which is the failure this exists to prevent.
        """
        last_status = "unknown"
        for attempt in range(self._cfg.fill_confirm_attempts):
            order = self.order_fill(order_id)
            if order is not None:
                last_status = str(order.get("status") or "unknown").upper()
                filled = _as_float_or_none(order.get("filledUsdtAmount"))
                if filled is None:
                    filled = _as_float_or_none(order.get("filledShareQty"))
                if last_status in self.DEAD_ORDER_STATUSES:
                    # The venue has answered, and the answer is no. Callers
                    # must not record a position on this.
                    raise OrderNotFilled(
                        f"order {order_id} did not fill: status "
                        f"{last_status}, filled {filled}")
                if last_status in self.FILLED_ORDER_STATUSES and filled:
                    return filled
                if filled and filled >= requested_usdt * self._cfg.min_fill_fraction:
                    return filled
            if attempt + 1 < self._cfg.fill_confirm_attempts:
                time.sleep(self._cfg.fill_confirm_delay_s)

        raise ApiError(
            f"could not confirm order {order_id} filled after "
            f"{self._cfg.fill_confirm_attempts} attempts (last status "
            f"{last_status}); refusing to record a position that may not exist")

    # -- settlement ---------------------------------------------------------

    def settled_outcome(self, rnd: Round
                        ) -> tuple[Side, float | None] | None:
        """
        Authoritative result: (winning side, realised PnL in USDT).

        Read from the venue's settled position history rather than
        reconstructed locally, because the market resolves on its own oracle.
        Returns None until the position has actually settled.
        """
        try:
            payload = self._request("settled_history", {
                "walletAddress": self.wallet().address,
                "limit": self._cfg.settled_history_limit})
        except ApiError as exc:
            # Returning None silently would be indistinguishable from "this
            # round has not settled yet", so a broken lookup would look like
            # an open position rather than a failure.
            LOG.warning("Settled-history lookup failed for %s: %s",
                        rnd.slug, exc)
            return None

        for pos in payload.get("positions") or []:
            if int(pos.get("marketTopicId") or -1) != rnd.topic_id:
                continue
            if pos.get("isWinner") is None:
                continue
            name = str(pos.get("outcomeName", "")).upper()
            held = Side.UP if name in ("YES", "UP") else Side.DOWN
            winner = held if pos.get("isWinner") else (
                Side.DOWN if held is Side.UP else Side.UP)
            pnl_raw = pos.get("realizedPnl")
            if pnl_raw is None:
                pnl_raw = pos.get("pnl")
            if pnl_raw is None:
                LOG.warning("Settled position for %s reports no realised PnL",
                            rnd.slug)
                return winner, None
            try:
                return winner, float(pnl_raw)
            except (TypeError, ValueError):
                LOG.warning("Unparseable realised PnL %r for %s",
                            pnl_raw, rnd.slug)
                return winner, None
        return None

    def batch_redeem(self, token_ids: list[str], chain_id: str) -> list[str]:
        """
        Claim winnings. Returns transaction hashes to poll.

        Winning outcome tokens are NOT credited automatically -- redemption is
        an explicit on-chain action. Skipping it makes the tradable balance
        appear to fall after every win.
        """
        if not token_ids:
            raise ValueError("token_ids must not be empty")
        wallet = self.wallet()
        payload = self._request("batch_redeem", {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "tokenIds": token_ids,
            "chainId": chain_id})

        hashes = []
        for res in payload.get("results") or []:
            # Only txHash exists in the schema. The `transactionHash` fallback
            # here was invented -- a field name that appears nowhere in the
            # connector, so it could never have matched and only served to
            # make the code look more tolerant than it was.
            tx = res.get("txHash")
            if tx:
                hashes.append(str(tx))
        if not hashes:
            if payload.get("batchId"):
                LOG.info("Redemption batch %s accepted, no tx hash yet",
                         payload["batchId"])
            else:
                # Accepted, no hash, no batch: the venue took the request and
                # had nothing to act on. On a winning position that means the
                # tokens are already gone -- redeemed elsewhere -- and the
                # caller must stop, not resubmit.
                raise NothingToRedeem(
                    f"nothing redeemable for {', '.join(token_ids)}")
        return hashes

    # A redemption transaction that will never land. The claim worker has to
    # tell these apart from "not confirmed yet": one wants a fresh attempt,
    # the other wants more patience.
    DEAD_REDEEM_STATUSES = frozenset({
        "FAILED", "FAIL", "REVERTED", "DROPPED", "REJECTED", "ERROR",
        "CANCELLED", "CANCELED", "EXPIRED",
    })

    def redeem_status(self, tx_hash: str) -> str | None:
        """Status of a redemption transaction, or None if unknown."""
        try:
            payload = self._request("redeem_status", {
                "walletAddress": self.wallet().address, "txHash": tx_hash})
        except ApiError as exc:
            LOG.debug("Redeem-status lookup failed for %s: %s", tx_hash, exc)
            return None
        status = payload.get("status")
        return None if status is None else str(status).upper()

    def final_price(self, rnd: Round) -> float | None:
        """variantData.endPrice once the round has resolved."""
        try:
            topic = self.market_detail(rnd.topic_id)
        except ApiError as exc:
            LOG.warning("Final-price lookup failed for %s: %s", rnd.slug, exc)
            return None
        vd = (topic.get("marketTopic") or topic).get("variantData") or {}
        raw = vd.get("endPrice") if isinstance(vd, dict) else None
        if raw is None:
            return None                 # genuinely not resolved yet
        try:
            price = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable endPrice %r for %s", raw, rnd.slug)
            return None
        if not math.isfinite(price):
            LOG.warning("Non-finite endPrice %r for %s", raw, rnd.slug)
            return None
        if price <= 0:
            LOG.warning("Implausible endPrice %r for %s", raw, rnd.slug)
            return None
        return price


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


class Journal:
    """Append-only record of every decision, for calibration analysis."""

    def __init__(self, path: str, profile: str = "unknown") -> None:
        self._profile = profile
        self._conn = sqlite3.connect(path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER, mode TEXT, slug TEXT, topic_id INTEGER, side TEXT,
                strike REAL, spot REAL, sigma REAL, seconds_left REAL,
                end_ms INTEGER, model_prob REAL, fill_price REAL, edge REAL,
                stake REAL, bankroll_before REAL, order_id TEXT,
                profile TEXT, buffer_z REAL, fee_bps INTEGER,
                symbol TEXT, trend_z REAL,
                resolved INTEGER DEFAULT 0, won INTEGER, pnl REAL,
                settle_source TEXT)""")
        # Journals predating the profile column stay readable.
        existing = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(trades)")}
        if "profile" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN profile TEXT")
        if "buffer_z" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN buffer_z REAL")
        if "fee_bps" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN fee_bps INTEGER")
        if "symbol" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN symbol TEXT")
        if "trend_z" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN trend_z REAL")
        self._conn.commit()

    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (ts, mode, slug, topic_id, side, strike, spot,"
            " sigma, seconds_left, end_ms, model_prob, fill_price, edge, stake,"
            " bankroll_before, order_id, profile, buffer_z, fee_bps, symbol,"
            " trend_z)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mode, rnd.slug, rnd.topic_id, sig.side.value,
             rnd.strike, spot, sigma, sig.seconds_left, rnd.end_ms,
             sig.model_prob, sig.fill_price, sig.edge, sig.stake_usdt,
             bankroll, order_id, self._profile, sig.buffer_z, rnd.fee_bps,
             rnd.symbol, sig.trend_z))
        self._conn.commit()
        return int(cur.lastrowid)

    def resolve(self, trade_id: int, won: bool, pnl: float,
                source: str) -> None:
        self._conn.execute(
            "UPDATE trades SET resolved=1, won=?, pnl=?, settle_source=?"
            " WHERE id=?", (1 if won else 0, pnl, source, trade_id))
        self._conn.commit()

    def diagnose(self, profile: str | None = None,
                 symbol: str | None = None) -> str:
        """
        Is the edge real, and if not, what would fix it?

        Answers one question per price bucket: was the realised win rate above
        the breakeven implied by the price paid? That single comparison
        decides everything. A losing run at a good win rate and a winning run
        at a poor one look identical over a few dozen trades, so the shortfall
        is reported with a standard error rather than as a bare number.
        """
        where = "WHERE resolved=1"
        args: tuple = ()
        if profile:
            where += " AND COALESCE(profile, 'unknown') = ?"
            args = (profile,)
        if symbol:
            where += " AND COALESCE(symbol, 'unknown') = ?"
            args = args + (symbol,)
        rows = self._conn.execute(
            f"SELECT fill_price, won, pnl, stake, fee_bps FROM trades {where}",
            args).fetchall()
        if not rows:
            return "No resolved trades yet."

        buckets: dict[int, list] = {}
        for price, won, pnl, stake, fee in rows:
            buckets.setdefault(int(price * 20), []).append(
                (price, won, pnl or 0.0, stake or 0.0, fee))

        scope = symbol or "all markets"
        out = [f"Trades analysed : {len(rows)}  ({scope})", "",
               "Realised win rate vs the breakeven for the price paid",
               "(breakeven uses each market's own published fee):",
               "  price band      n   needed   actual     gap      P&L  verdict"]
        total_gap_n = 0
        verdicts: list[tuple[str, float, int]] = []

        for b in sorted(buckets):
            vals = buckets[b]
            n = len(vals)
            avg_price = sum(v[0] for v in vals) / n
            # Each market publishes its own fee; assuming 2% shifts the exact
            # bar this whole verdict is measured against. Rows predating the
            # column fall back to the configured default.
            fees = [v[4] for v in vals if v[4] is not None]
            fee = int(sum(fees) / len(fees)) if fees else DEFAULT_FEE_BPS
            needed = breakeven_probability(avg_price, fee)
            actual = sum(v[1] for v in vals) / n
            pnl = sum(v[2] for v in vals)
            gap = actual - needed
            se = math.sqrt(max(actual * (1 - actual), EPS) / n)

            if n < 20:
                verdict = "too few"
            elif gap > 2 * se:
                verdict = "EDGE"
            elif gap < -2 * se:
                verdict = "NO EDGE"
            else:
                verdict = "unclear"
            verdicts.append((verdict, gap, n))
            total_gap_n += n
            out.append(f"  {b/20:.2f}-{b/20+0.05:.2f} {n:>6} {needed:>8.1%} "
                       f"{actual:>8.1%} {gap:>+7.1%} {pnl:>+8.2f}  {verdict}")

        out += ["", "=" * 66, "WHAT THIS MEANS", "=" * 66]
        losing = [v for v in verdicts if v[0] == "NO EDGE"]
        winning = [v for v in verdicts if v[0] == "EDGE"]
        unclear = [v for v in verdicts if v[0] in ("unclear", "too few")]

        if losing and not winning:
            out += [
                "",
                "Your win rate is significantly BELOW breakeven. Raising the",
                "stake cannot fix this: stake multiplies expected value, it",
                "cannot change its sign. A bigger bet on a negative edge just",
                "loses faster.",
                "",
                "The levers that do work, in order of directness:",
                "  1. Demand a bigger buffer (--min-buffer 2.5 or 3.0). More",
                "     standard deviations from the strike means a genuinely",
                "     higher win probability, not just a higher price.",
                "  2. Enter later (lower entry_window_start_s). The same",
                "     buffer is worth more with less time left to reverse.",
                "  3. Pay less (lower max_entry_price). A worse win rate but a",
                "     better win:loss ratio, and a lower bar to clear.",
                "  4. Stop. If none of the above lifts the actual column above",
                "     the needed column, the edge is not there to be found.",
            ]
        elif winning and not losing:
            out += [
                "",
                "Your win rate is significantly ABOVE breakeven in the bands",
                "marked EDGE. Sizing up there is correct, and is what Kelly",
                "already does automatically -- the bot raises stake as the",
                "edge grows, without any change from you.",
                "",
                "Do NOT raise the stake cap by hand to 'cover' losses. The",
                "losses are already priced in; the cap is what keeps a run of",
                "them survivable.",
            ]
        else:
            out += [
                "",
                (f"Inconclusive: {len(unclear)} band(s) lack the sample to call,"
                f" {len(winning)} show edge, {len(losing)} show none."),
                "",
                "Over a few dozen trades a 68% win rate and a 76% win rate are",
                "indistinguishable, yet one loses money and the other compounds.",
                "Keep the settings fixed and let the sample grow. Changing size",
                "in response to a losing streak is the one move that converts",
                "an unclear result into a certain loss.",
            ]
        return "\n".join(out)

    def calibration_report(self, profile: str | None = None) -> str:
        """
        Report per profile, or every profile in turn.

        Profiles trade disjoint price bands, so pooling them averages a
        longshot strategy together with a favourite strategy and reports a
        bias that belongs to neither.
        """
        profiles = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT COALESCE(profile, 'unknown') FROM trades "
            "WHERE resolved=1 ORDER BY 1")]
        if not profiles:
            return "No resolved trades yet. Let paper mode run first."

        if profile is None and len(profiles) > 1:
            parts = [(f"Journal contains {len(profiles)} profiles: "
                     f"{', '.join(profiles)}"), ""]
            for name in profiles:
                parts.append(f"{'=' * 62}\nPROFILE: {name}\n{'=' * 62}")
                parts.append(self.calibration_report(name))
                parts.append("")
            return "\n".join(parts)

        target = profile or profiles[0]
        rows = self._conn.execute(
            "SELECT model_prob, won, pnl, stake FROM trades "
            "WHERE resolved=1 AND COALESCE(profile, 'unknown') = ?",
            (target,)).fetchall()
        if not rows:
            return f"No resolved trades for profile {target!r}."

        buckets: dict[int, list[tuple[float, int]]] = {}
        for prob, won, _, _ in rows:
            buckets.setdefault(min(int(prob * 10), 9), []).append((prob, won))

        n = len(rows)
        markets = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT COALESCE(symbol, 'unknown') FROM trades "
            "WHERE resolved=1 AND COALESCE(profile, 'unknown') = ? ORDER BY 1",
            (target,))]
        header = [f"Profile         : {target}",
                  f"Markets         : {', '.join(markets)}"]
        pnl = sum(r[2] or 0.0 for r in rows)
        staked = sum(r[3] or 0.0 for r in rows)
        lines = header + [
            f"Resolved trades : {n}",
            f"Total P&L       : {pnl:+.2f} USDT",
            f"Return on stake : {(pnl / staked if staked else 0):+.2%}",
            f"Hit rate        : {sum(r[1] for r in rows) / n:.1%}",
            "",
            "Calibration (model says X% -> actually won Y%):",
            "  bucket        n   predicted    actual      gap",
        ]
        for b in sorted(buckets):
            vals = buckets[b]
            pred = sum(p for p, _ in vals) / len(vals)
            act = sum(w for _, w in vals) / len(vals)
            se = math.sqrt(max(act * (1 - act), EPS) / len(vals))
            flag = "" if abs(act - pred) <= 2 * se else "  <-- off"
            lines.append(f"  {b*10:>3}-{b*10+9:<3} {len(vals):>6}"
                         f"   {pred:>8.1%} {act:>9.1%} {act-pred:>+8.1%}{flag}")
        # Market-price buckets: does the venue's own price predict outcomes?
        price_rows = self._conn.execute(
            "SELECT fill_price, won FROM trades WHERE resolved=1 "
            "AND COALESCE(profile, 'unknown') = ?", (target,)).fetchall()
        pbuckets: dict[int, list[tuple[float, int]]] = {}
        for price, won in price_rows:
            pbuckets.setdefault(min(int(price * 10), 9), []).append((price, won))

        if len(markets) > 1:
            lines += ["", "Per market (each trades independently):",
                      "  market            n   hit rate       P&L"]
            for mk in markets:
                mrows = self._conn.execute(
                    "SELECT won, pnl FROM trades WHERE resolved=1 "
                    "AND COALESCE(profile, 'unknown') = ? "
                    "AND COALESCE(symbol, 'unknown') = ?",
                    (target, mk)).fetchall()
                if not mrows:
                    continue
                wins = sum(w for w, _ in mrows)
                pnl_m = sum(p or 0.0 for _, p in mrows)
                lines.append(f"  {mk:<12} {len(mrows):>6} "
                             f"{wins/len(mrows):>9.1%} {pnl_m:>+9.2f}")

        lines += [
            "",
            "Favourite-longshot bias (market price vs realised frequency):",
            "  price       n   implied     actual      gap",
        ]
        if n < 30:
            lines.append(f"  (only {n} trade(s): far too few to read a bias "
                         f"from -- shown for completeness, not as a verdict)")
        for b in sorted(pbuckets):
            vals = pbuckets[b]
            imp = sum(p for p, _ in vals) / len(vals)
            act = sum(w for _, w in vals) / len(vals)
            se = math.sqrt(max(act * (1 - act), EPS) / len(vals))
            flag = ""
            if abs(act - imp) > 2 * se:
                flag = "  <-- underpriced" if act > imp else "  <-- overpriced"
            lines.append(f"  {b/10:.1f}-{b/10+0.1:.1f} {len(vals):>6}"
                         f"   {imp:>8.1%} {act:>9.1%} {act-imp:>+8.1%}{flag}")
        lines += [
            "",
            "A positive gap means contracts at that price win MORE often than",
            "their price implies -- that band is underpriced and worth buying.",
            "If high prices show positive gaps and low prices negative ones,",
            "the market has a favourite-longshot bias and buying favourites is",
            "the right side. The reverse favours the convex profile. This is",
            "the measurement that decides between them; nothing else does.",
        ]

        # Buffer buckets: is the model reliable where it claims near-certainty?
        z_rows = self._conn.execute(
            "SELECT buffer_z, won, pnl FROM trades WHERE resolved=1 "
            "AND buffer_z IS NOT NULL AND buffer_z != 0 "
            "AND COALESCE(profile, 'unknown') = ?", (target,)).fetchall()
        if z_rows:
            edges = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 1e9)]
            lines += [
                "",
                "By buffer (|z| = distance from strike in sigmas of time left):",
                "  buffer        n   win rate      P&L",
            ]
            for lo, hi in edges:
                vals = [(w, p or 0.0) for zz, w, p in z_rows
                        if lo <= abs(zz) < hi]
                if not vals:
                    continue
                wins = sum(w for w, _ in vals)
                pnl = sum(p for _, p in vals)
                label = f"{lo}-{hi}" if hi < 1e9 else f"{lo}+"
                lines.append(f"  {label:<8} {len(vals):>6} "
                             f"{wins/len(vals):>9.1%} {pnl:>+9.2f}")
            lines += [
                "",
                "A big buffer should show a high win rate AND positive P&L. A",
                "high win rate with negative P&L means the wins are too small",
                "to pay for the rare losses -- the exact failure mode of",
                "trading near-certainties, and only this table reveals it.",
            ]

        lines += [
            "",
            "If 'actual' sits consistently below 'predicted', the model is",
            "overconfident and every edge estimate is inflated. Do not trade",
            "real money until the gap column is small and unbiased across",
            "buckets over several hundred trades. Rows flagged '<-- off' differ",
            "from prediction by more than two standard errors.",
        ]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Strategy (pure)
# --------------------------------------------------------------------------


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
    return min(stake * cfg.trend_stake_multiple, ceiling)


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
        if not (cfg.min_entry_price <= avg <= cfg.max_entry_price):
            blocked = _worse(blocked, "price outside the entry band")
            continue

        if not clears_edge(model_prob, avg, cfg, fee_bps):
            blocked = _worse(blocked, "edge below the floor")
            continue
        # The venue quotes to its own precision, so a fill price carrying
        # more digits than that is fiction. Snap before pricing the edge.
        avg = rnd.round_price(avg)
        if not 0.0 < avg < 1.0:
            blocked = _worse(blocked, "price outside the entry band")
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


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class Trader:
    """
    The trading loop.

    Installs a SIGTERM handler because hosted platforms (Render, Fly, Heroku)
    send SIGTERM on every deploy and restart, and Python does NOT turn that
    into KeyboardInterrupt -- the process simply dies. Without this an open
    position is abandoned mid-round and left unresolved in the journal, which
    silently corrupts the calibration record on every redeploy.
    """

    def __init__(self, cfg: Config | ConfigStore) -> None:
        self._store: ConfigStore | None = None
        self._static_cfg: Config | None = None
        if isinstance(cfg, Config):
            self._static_cfg = cfg
        else:
            self._store = cfg
        # Resolve once for construction. A ConfigStore has no config fields of
        # its own, so reading cfg.db_path off the argument would fail; every
        # later read goes through the _cfg property and sees reloads.
        config = self._resolve(cfg)
        self._client = PredictionClient(cfg)
        self._vol = VolatilityEstimator(cfg, self._client.session)
        self._journal = Journal(config.db_path, config.profile_name)
        self._paper_bankroll = config.paper_start_bankroll
        # Per-symbol so one market's losing streak cannot gate another's
        # trading. The bankroll and the daily loss limit stay shared, because
        # there is only one account.
        self._risk: dict[str, RiskManager] = {}
        self._account_risk: RiskManager | None = None
        self._seen: dict[int, int] = {}
        # topic_id -> (end_ms, latest reason this round is not tradable).
        # A live round sits here and is re-examined on every poll; once it
        # expires the reason is tallied and the entry dropped.
        self._watching: dict[int, tuple[int, str]] = {}
        self._missed: dict[str, int] = {}
        self._missed_total = 0
        # Last "why am I not trading" message, so it is logged on change
        # rather than on every poll.
        self._idle_reason = ""
        # Keyed by (symbol, side) rather than just symbol: every model-based
        # profile only ever opens one side per symbol, so this changes
        # nothing for them, but it lets the straddle profile hold BOTH
        # sides of the same symbol/round as two independent entries.
        self._positions: dict[tuple[str, Side], Position] = {}
        self._hydrated: dict[int, Round] = {}
        self._errors = 0
        # token_id -> (expected payout USDT, tx hashes). Counted toward the
        # bankroll so an unclaimed win is not misread as a drawdown.
        self._unredeemed: dict[str, tuple[float, list[str], str]] = {}
        # A claim is handed to a background worker (see _claim) so that
        # settling a win never makes the trading loop wait on a chain
        # confirmation before it looks at the next round. The lock guards
        # every mutation of _unredeemed, since it is now written from two
        # threads; the worker itself is started lazily, on the first win.
        self._claim_lock = threading.Lock()
        self._claim_queue: "queue.Queue[Position]" = queue.Queue()
        self._claim_thread: threading.Thread | None = None
        self._stopping = False
        self._settled_count = 0
        # The mode actually in force. Diverges from the config only while a
        # position is open and a switch is pending.
        self._active_live = config.live

    @staticmethod
    def _resolve(cfg: Config | ConfigStore) -> Config:
        return cfg if isinstance(cfg, Config) else cfg.current

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @property
    def _position(self) -> Position | None:
        """The single open position, when exactly one market is configured."""
        if len(self._positions) == 1:
            return next(iter(self._positions.values()))
        return None

    @_position.setter
    def _position(self, value: Position | None) -> None:
        if value is None:
            self._positions.clear()
        else:
            self._positions[(value.rnd.symbol, value.signal.side)] = value

    def _risk_for(self, symbol: str) -> RiskManager:
        """Streak and calibration state for one market, created on demand."""
        if symbol not in self._risk:
            source = self._store or self._cfg
            self._risk[symbol] = RiskManager(source, self._bankroll())
        return self._risk[symbol]

    def _committed(self) -> float:
        return sum(p.committed_usdt for p in self._positions.values())

    def _outstanding(self) -> float:
        """
        The bot's own money in flight: staked, or won but not yet credited.

        Both halves have to be counted. Reconciliation compares the balance
        against cost-basis arithmetic, and either one of these makes those
        two disagree by an amount that is the bot's doing -- see
        RiskManager.reconcile for what mistaking it for an external flow
        did to the daily baseline.
        """
        with self._claim_lock:
            pending = sum(v for v, _, _ in self._unredeemed.values())
        return self._committed() + pending

    def _available(self, bankroll: float) -> float:
        """
        Bankroll that may back a NEW position.

        Sizing against the full balance while other markets already hold
        positions is how N concurrent trades quietly become N times the
        intended exposure. The reserve keeps some powder dry regardless.
        """
        return max(0.0, bankroll * (1.0 - self._cfg.reserve_pct)
                   - self._committed())

    @property
    def _live(self) -> bool:
        """
        The mode in force right now.

        Reads the ACTIVE mode rather than the configured one, so a pending
        switch cannot take effect halfway through a round. Every decision that
        turns on paper-vs-live goes through here.
        """
        return self._active_live

    def _apply_pending_mode(self) -> None:
        """Adopt a configured mode change, but only while flat."""
        wanted = self._cfg.live
        if wanted == self._active_live:
            return
        if self._positions or self._unredeemed:
            LOG.info("Mode change to %s is pending: waiting until flat "
                     "(open position or unclaimed winnings)",
                     "LIVE" if wanted else "PAPER")
            return

        self._active_live = wanted
        # The bankroll means something different in each mode, so the risk
        # baseline and streak counters would otherwise carry across a switch
        # and misreport the first day in the new mode.
        try:
            bankroll = self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Switched to %s but could not read the balance: %s",
                      "LIVE" if wanted else "PAPER", exc)
            return
        self._risk = {}
        self._account_risk = RiskManager(self._store or self._cfg, bankroll)
        LOG.warning("MODE NOW %s -- bankroll %.2f, risk counters reset",
                    "LIVE" if wanted else "PAPER", bankroll)

    def _correct_pnl(self, symbol: str, delta: float) -> None:
        """
        Push a venue-measured correction into every manager that books PnL.

        Both of them or neither: the per-market manager owns the streak and
        the account manager owns the daily limit, and a correction that
        reached only one would leave the two disagreeing about the same day.
        """
        if abs(delta) <= EPS:
            return
        self._risk_for(symbol).correct_realised_pnl(delta)
        if self._account_risk is not None:
            self._account_risk.correct_realised_pnl(delta)

    def _bankroll(self) -> float:
        """
        Tradable balance, including winnings that are settled but not yet
        credited on chain. Excluding them would make a winning streak look
        like a drawdown and falsely trip the daily loss limit.
        """
        if not self._live:
            return self._paper_bankroll

        # The API reading is authoritative. Unclaimed winnings are added only
        # when explicitly configured, because the portfolio endpoint already
        # includes settled positions and counting them twice inflates the
        # bankroll by the gross payout.
        balance = self._client.balance_usdt()
        if not self._cfg.count_unredeemed_in_bankroll:
            return balance
        # Snapshot under the lock: this dict is now written from the claim
        # worker thread too, and iterating it live could race a mutation.
        with self._claim_lock:
            pending = sum(v for v, _, _ in self._unredeemed.values())
        return balance + pending

    def _start_claim_worker(self) -> None:
        """Start the background redemption worker, once, on first use."""
        if self._claim_thread is not None and self._claim_thread.is_alive():
            return
        self._claim_thread = threading.Thread(
            target=self._claim_worker_loop, name="claim-worker", daemon=True)
        self._claim_thread.start()

    def _claim_worker_loop(self) -> None:
        """Consume winning positions and chase each redemption to landing."""
        while True:
            pos = self._claim_queue.get()
            try:
                self._claim_relentlessly(pos)
            except Exception:                # noqa: BLE001 - see below
                # A bug in the claim path must not silently strand a real
                # win: log it loudly and keep the worker alive for the next
                # one rather than letting an uncaught exception kill the
                # thread out from under the trading loop.
                LOG.exception("Claim worker error settling %s", pos.rnd.slug)
            finally:
                self._claim_queue.task_done()

    def _forget_claim(self, token_id: str) -> None:
        """
        Stop tracking a win the venue says is no longer redeemable.

        Almost always the operator claimed it by hand. The bot's only proof
        of a landed claim is the status of a tx hash it submitted itself, so
        a redemption performed anywhere else is invisible to it: without this
        the worker resubmits until claim_timeout_s and then keeps the token
        in _unredeemed for the life of the process, where it inflates
        _outstanding, blocks every reconciliation, and pins a pending mode
        switch on winnings that were credited long ago.

        Callers log the reason themselves. Which of the two ways the venue
        said "there is nothing here" is the only interesting part of this
        event, and burying it one frame deeper hid it from the reader and
        from the meta-test that insists a handler explain what it swallowed.
        """
        with self._claim_lock:
            self._unredeemed.pop(token_id, None)

    def _claim_relentlessly(self, pos: Position) -> None:
        """
        Redeem one winning position and keep trying until it is confirmed.

        This runs off the main thread specifically so the trading loop never
        waits on it: it can take anywhere from about a second to roughly a
        minute for a claim to land on chain, and every one of those seconds
        is a second the next round's open is unwatched. Retrying on a short,
        dedicated interval -- rather than once per poll_interval_s tick --
        is what gets the balance freed up for re-entry as early as possible.
        """
        token_id = pos.rnd.token_for(pos.signal.side)
        payout = pos.signal.stake_usdt / pos.signal.fill_price
        chain_id = pos.rnd.chain_id
        deadline = time.time() + self._cfg.claim_timeout_s
        hashes: list[str] = []

        while time.time() < deadline:
            if not hashes:
                try:
                    hashes = self._client.batch_redeem([token_id], chain_id)
                    with self._claim_lock:
                        self._unredeemed[token_id] = (payout, hashes,
                                                      chain_id)
                    LOG.info("Redeeming %.2f USDT (tx %s)", payout,
                             ", ".join(hashes) or "pending")
                except NothingToRedeem as exc:
                    LOG.warning("Nothing left to redeem for %s: treating "
                                "%.2f USDT as already credited (claimed "
                                "outside this bot?) -- %s",
                                pos.rnd.slug, payout, exc)
                    self._forget_claim(token_id)
                    return
                except (ApiError, requests.RequestException) as exc:
                    if _is_already_redeemed(exc):
                        LOG.warning("Venue refuses to redeem %s because it "
                                    "is already claimed: treating %.2f USDT "
                                    "as credited -- %s",
                                    pos.rnd.slug, payout, exc)
                        self._forget_claim(token_id)
                        return
                    LOG.debug("Redeem attempt for %s failed, retrying: %s",
                              token_id, exc)
                    time.sleep(self._cfg.claim_poll_interval_s)
                    continue

            if hashes:
                try:
                    statuses = [self._client.redeem_status(h)
                               for h in hashes]
                except (ApiError, requests.RequestException) as exc:
                    LOG.debug("Status check for %s failed, retrying: %s",
                              token_id, exc)
                    time.sleep(self._cfg.claim_poll_interval_s)
                    continue
                if statuses and all(
                        s in ("SUCCESS", "CONFIRMED", "COMPLETED")
                        for s in statuses):
                    with self._claim_lock:
                        self._unredeemed.pop(token_id, None)
                    LOG.info("Redemption confirmed: %.2f USDT credited (%s)",
                             payout, pos.rnd.slug)
                    return
                if any(s in PredictionClient.DEAD_REDEEM_STATUSES
                       for s in statuses if s):
                    # The transaction is not coming back. Forget the hash and
                    # go round again: either the resubmission succeeds, or
                    # batch_redeem answers that there is nothing left to
                    # claim -- which is what a hand-redemption racing the
                    # bot's own transaction looks like from here.
                    LOG.warning("Redemption tx for %s reported %s; "
                                "resubmitting", pos.rnd.slug,
                                ", ".join(s or "?" for s in statuses))
                    hashes = []
                    with self._claim_lock:
                        self._unredeemed[token_id] = (payout, [], chain_id)

            time.sleep(self._cfg.claim_poll_interval_s)

        LOG.warning("Redemption for %s not confirmed within %.0fs; still "
                    "tracked as unredeemed and will keep being retried the "
                    "next time this token is claimed", pos.rnd.slug,
                    self._cfg.claim_timeout_s)

    def _claim(self, pos: Position) -> None:
        """
        Hand a winning position to the background claim worker.

        Returns immediately -- not waiting for the chain -- which is the
        entire point: the trading loop keeps scanning for the next round's
        open the instant this one settles, while the worker chases the
        actual redemption on its own schedule in the background.
        """
        token_id = pos.rnd.token_for(pos.signal.side)
        payout = pos.signal.stake_usdt / pos.signal.fill_price
        with self._claim_lock:
            self._unredeemed[token_id] = (payout, [], pos.rnd.chain_id)
        self._start_claim_worker()
        self._claim_queue.put(pos)

    def _prune(self, now_ms: int) -> None:
        for tid in [t for t, end in self._seen.items()
                    if end < now_ms - self._cfg.prune_after_s * 1000]:
            self._seen.pop(tid, None)
            self._hydrated.pop(tid, None)

    def _tally_missed(self, now_ms: int) -> None:
        """
        Count rounds that expired without a trade, by what blocked them.

        Silence is the failure mode this exists to prevent. With a return
        floor in force, "no trade" is the correct answer surprisingly often,
        and it is indistinguishable from a broken endpoint or a stale book
        unless the reason is written down. A periodic summary makes the
        difference between "the market never offered a price worth taking"
        and "the buffer gate is set too high to ever fire" visible without
        having to read a debug log.
        """
        expired = [t for t, (end, _) in self._watching.items() if end < now_ms]
        for tid in expired:
            _, reason = self._watching.pop(tid)
            self._missed[reason] = self._missed.get(reason, 0) + 1
            self._missed_total += 1

        if not expired or self._missed_total % 25 != 0:
            return
        ranked = sorted(self._missed.items(), key=lambda kv: -kv[1])
        LOG.info("No trade in %d round(s) so far: %s", self._missed_total,
                 "; ".join(f"{n} {reason}" for reason, n in ranked[:4]))
        top = ranked[0][0]
        if top == "win pays less than the return floor":
            LOG.info("  The prices on offer were fine bets but small wins. "
                     "Lower min_win_return to trade more of them, "
                     "understanding that is the trade you asked not to make.")
        elif top == "buffer too small for the time left":
            LOG.info("  Spot is not moving far enough from the strike. "
                     "Lower min_buffer_sigmas, or accept fewer setups.")
        elif top == "both straddle payouts do not beat the stake":
            LOG.info("  The two sides are priced to sum to 1.00 or more, so "
                     "buying both is a guaranteed loss. This is the normal "
                     "resting state of the straddle profile -- it is "
                     "refusing rounds, not failing to see them. Nothing to "
                     "fix unless it never clears.")
        elif top == "straddle leg below the venue minimum":
            LOG.info("  Sizing the pair by payout put one leg under the "
                     "venue minimum. Raise straddle_stake_pct, or fund the "
                     "wallet, so the cheap side still clears it.")
        elif top == "no side has reached the price floor":
            LOG.info("  The leader never reached %.2f while the floor was "
                     "still in force. Reaching expiry on that reason also "
                     "means the fallback never got a look -- no poll landed "
                     "inside its window, usually because the position slots "
                     "were full or the market was halted. Lower "
                     "last_minute_price_floor to take more of these before "
                     "the fallback has to.",
                     self._cfg.last_minute_price_floor)
        elif top == "the leading side is priced above the ceiling":
            LOG.info("  The rounds reaching the last minute were already "
                     "decided, and %.2f is the most this profile will pay "
                     "for one. That is the ceiling doing its job, not a "
                     "fault. Raise last_minute_max_price to take them, "
                     "understanding a win at 0.97 pays about 3%%.",
                     self._cfg.last_minute_max_price)
        elif top == "the two sides are priced level":
            LOG.info("  The book could not separate UP from DOWN in the last "
                     "minute. There is no dominant side to buy in that, and "
                     "picking one anyway would be inventing a signal. This "
                     "is the profile refusing rounds, not failing to see "
                     "them.")
        elif top == "edge below the floor":
            LOG.info("  The venue is pricing these rounds close to the "
                     "model. That is a market with no edge in it, not a "
                     "misconfiguration.")

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            name = signal.Signals(signum).name
            if self._stopping:
                LOG.warning("%s again; exiting immediately", name)
                raise SystemExit(1)
            self._stopping = True
            LOG.info("%s received; finishing the open position then stopping",
                     name)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread, or the platform lacks the signal.
                LOG.debug("Could not install a handler for %s", sig)

    def run(self) -> None:
        self._install_signal_handlers()
        try:
            self._client.sync_clock()
            if self._live:
                w = self._client.wallet()
                LOG.info("Prediction wallet %s", w.address)
                try:
                    quota = self._client.remaining_quota_usdt()
                    if quota is not None:
                        LOG.info("Remaining daily quota: %.2f USDT", quota)
                except ApiError as exc:
                    LOG.warning("Could not read daily quota: %s", exc)
            bankroll = self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Startup failed: %s", exc)
            LOG.error("Run --preflight to diagnose.")
            return

        self._account_risk = RiskManager(self._store or self._cfg, bankroll)
        LOG.info("Starting %s mode. Bankroll %.2f USDT. Markets: %s",
                 "LIVE" if self._live else "PAPER",
                 bankroll, ", ".join(self._cfg.symbols) or
                 "every 5m up/down market (auto-discovered)")
        last_sync = time.time()

        try:
            while True:
                if self._stopping:
                    raise Shutdown("stop signal received")
                try:
                    if self._store is not None and self._store.maybe_reload():
                        # Parsing reads class attributes, so refresh them
                        # whenever the configuration swaps.
                        PredictionClient.apply_config(self._cfg)
                    if time.time() - last_sync > self._cfg.clock_resync_s:
                        self._client.sync_clock()
                        last_sync = time.time()

                    # Redemption is no longer polled from here: a background
                    # worker (started in _claim, on the first win) chases
                    # every claim to confirmation on its own tight schedule,
                    # specifically so this loop never pauses on a chain
                    # confirmation before looking at the next round.
                    self._settle_open()
                    # Driven from the loop, not from _maybe_enter: that
                    # returns early while a position is open, and the rounds
                    # passing unwatched during those minutes are exactly the
                    # ones worth counting.
                    self._tally_missed(self._client.now_ms())
                    bankroll = self._bankroll()
                    # Account-level limits: one balance, one daily loss cap.
                    # Per-market streaks are checked inside _maybe_enter.
                    self._account_risk.check(bankroll, self._outstanding())
                    self._apply_pending_mode()
                    self._maybe_scale_in_all(bankroll)
                    self._maybe_enter(bankroll,
                                      "LIVE" if self._live else "PAPER")
                    self._errors = 0
                except (TradingHalted, Shutdown):
                    raise
                # sqlite3.Error and OSError belong here for the same reason
                # ApiError does: a full disk, a briefly locked journal or a
                # socket the requests layer did not wrap are all transient
                # conditions that the backoff-then-halt path already handles
                # correctly. Left out, each one killed the process outright
                # while a position was open.
                except (ApiError, requests.RequestException,
                        sqlite3.Error, OSError) as exc:
                    self._errors += 1
                    LOG.warning("Recoverable error %d/%d: %s", self._errors,
                                self._cfg.max_consecutive_errors, exc)
                    if self._errors >= self._cfg.max_consecutive_errors:
                        raise TradingHalted(
                            "too many consecutive API errors; check "
                            "connectivity and endpoint paths") from exc
                    time.sleep(min(self._cfg.error_backoff_max_s,
                                   2.0 ** min(self._errors, 5)))

                time.sleep(self._cfg.poll_interval_s)

        except TradingHalted as exc:
            LOG.error("HALTED: %s", exc)
            if self._positions:
                LOG.info("Waiting for %d open position(s) to resolve...",
                         len(self._positions))
                self._drain()
            LOG.error("Stopped. Review the journal before restarting.")
        except Shutdown:
            if self._positions:
                LOG.info("Draining %d open position(s) before exit...",
                         len(self._positions))
                self._drain()
            LOG.info("Clean shutdown; no position abandoned.")
        except KeyboardInterrupt:
            LOG.info("Interrupted; open position left in the journal.")
        except Exception:                    # noqa: BLE001 - see below
            # A bug is not a reason to walk away from money already staked.
            # Every unanticipated exception previously escaped run(), killed
            # the process and abandoned the open position: the journal row
            # stayed unresolved and, in live mode, a winning token went
            # unclaimed. Catching here costs nothing -- the bot still stops,
            # and still stops loudly -- but it stops AFTER the position has
            # been settled and claimed.
            #
            # Deliberately not catching BaseException: SystemExit from the
            # second interrupt signal means "leave now", and KeyboardInterrupt
            # is handled above.
            LOG.exception("Unexpected error; shutting down")
            if self._positions:
                LOG.info("Draining %d open position(s) before exit...",
                         len(self._positions))
                try:
                    self._drain()
                except Exception:            # noqa: BLE001
                    LOG.exception("Drain failed; positions remain open")
            raise

    def _maybe_enter(self, bankroll: float, mode: str) -> None:
        """Dispatch to whichever entry strategy the config selects."""
        if self._cfg.straddle:
            self._maybe_enter_straddle(bankroll, mode)
        elif self._cfg.last_minute:
            self._maybe_enter_last_minute(bankroll, mode)
        else:
            self._maybe_enter_model(bankroll, mode)

    def _straddle_payouts_clear(
            self, raw: Round, prices: dict[Side, float],
            stakes: dict[Side, float],
            total: float) -> tuple[bool, float, str]:
        """
        Would this pair pay back more than it cost, whichever way it lands?

        Returns (ok, worst_case_pnl, reason). `reason` is empty when ok, and
        otherwise names the gate that bound, in the vocabulary _tally_missed
        reports.

        The question is about PAYOUTS, not prices. A leg staking `s` at
        price `p` returns s/p net of fee if it wins, and the pair is only
        worth holding when BOTH of those returns exceed the `total` staked
        across the two -- that is what makes the round's outcome irrelevant,
        which is the entire point of a straddle. straddle_worst_case_pnl
        already computes min(payout_up, payout_down) - total, so the test is
        simply that it come out positive.

        The comparison is strict. "Pays back what it cost" is not the same
        as "pays more than it cost", and at exactly break-even the round is
        capital at risk for nothing.
        """
        for side, price in prices.items():
            if not 0.0 < price <= self._cfg.straddle_max_leg_price:
                return False, 0.0, "straddle leg priced above ceiling"
            if stakes[side] < self._cfg.min_stake_usdt:
                # Reachable through the split, not just through a small
                # bankroll: weighting an asymmetric pair by payout can put
                # the cheap leg under the venue minimum even when the pair
                # as a whole is well funded.
                return False, 0.0, "straddle leg below the venue minimum"

        worst = straddle_worst_case_pnl(
            stakes[Side.UP], stakes[Side.DOWN], prices[Side.UP],
            prices[Side.DOWN], raw.fee_bps)
        if (self._cfg.straddle_require_positive_worst_case
                and worst <= total * self._cfg.straddle_min_worst_case_return):
            return False, worst, "both straddle payouts do not beat the stake"
        return True, worst, ""

    def _book_price(self, raw: Round, side: Side) -> float | None:
        """
        Best ask for one side, or None when there is no usable price.

        None is not an error, it is the ordinary state of a market late in a
        round: the side that is losing decays toward 0.00 and the side that
        is winning toward 1.00, and rounding to the market's own precision
        snaps both the rest of the way. Neither is a probability -- 1.00 can
        never pay back more than it cost and 0.00 has no payout defined at
        all -- so every price formula downstream rejects them, and the one
        in _complete_half_straddles rejected them by raising ValueError out
        of breakeven_probability and killing the process.

        Returning None instead says the same thing without pretending the
        number is tradable, and without clamping it into range, which would
        quietly turn "this side is worthless" into "this side is a bargain".
        """
        price = raw.round_price(self._raw_book_price(raw, side))
        return price if 0.0 < price < 1.0 else None

    def _raw_book_price(self, raw: Round, side: Side) -> float:
        """
        Best ask for one side, before rounding and before the usability test.

        Split out from _book_price because RANKING the two sides and BUYING
        one of them are different questions. A side that has decayed to 0.004
        is not tradable and _book_price is right to answer None, but it is
        still unambiguously the cheaper of the two -- and a caller asking
        "which side is the book calling the winner?" needs that answer, not a
        None it would have to guess the direction of.
        """
        levels = self._client.asks_for(raw, side)
        return (levels[0][0] if levels
                else min(raw.quote_for(side)
                         * (1.0 + self._cfg.assumed_spread_pct), 0.999))

    def _place_leg(self, raw: Round, side: Side, price: float, stake: float,
                   quote: "Quote | None" = None
                   ) -> tuple[float, float, str | None] | None:
        """
        Buy one side. Returns (fill price, stake filled, order id), or None.

        None means the venue said the order did not fill, so there is no
        position and the caller must not record one. A MARKET FOK order that
        cannot fill is killed while still returning an order id, so the id
        alone proves nothing.

        The two confirmation failures are deliberately NOT treated alike. A
        dead status is knowledge: no position, nothing to record, and
        recording one would invent a trade that later settles and books a
        profit never made. A timeout or a dropped socket is the absence of
        knowledge: the order may well be live, and dropping it strands real
        money that is never settled and never claimed. So the first returns
        None and the second records at the requested size, loudly.

        Paper mode fills at the passed price and places nothing.
        """
        order_id = None
        if self._live:
            if quote is None:
                quote = self._client.get_quote(raw, side, stake)
            order_id = self._client.place_order(raw, quote, stake)
            price = quote.average_price
            if self._cfg.confirm_fills:
                try:
                    stake = self._client.confirm_fill(order_id, stake)
                except OrderNotFilled as exc:
                    LOG.warning("%s: the %s order did not fill (%s); no "
                                "position recorded", raw.slug, side.value,
                                exc)
                    return None
                except (ApiError, requests.RequestException) as exc:
                    LOG.error("%s: the %s order was placed but the fill could "
                              "NOT be confirmed either way (%s); recording it "
                              "at the requested %.2f USDT so it is settled "
                              "and claimed rather than stranded",
                              raw.slug, side.value, exc, stake)
            if quote.fee_usdt > 0:
                LOG.debug("%s %s fee %.4f USDT", raw.slug,
                          side.value, quote.fee_usdt)
        return price, stake, order_id

    def _completion_is_worth_waiting_out(self, raw: Round, be_open: float,
                                         be_other: float,
                                         seconds_left: float) -> bool:
        """
        Is a price that already locks the round in still worth refusing?

        Any second leg with be_other < 1 - be_open guarantees the round, but
        they are not equally good: cheaper is more profit. Early in a round
        there is time to hold out; as the hedge deadline approaches there is
        not, and a smaller locked-in profit beats an open directional bet.
        The bar slides between the two rather than sitting at either extreme,
        because a fixed high bar strands positions and a fixed low one takes
        the first crumb offered.

        What it slides FROM is the open leg's own fill price. Equal stakes at
        equal prices pay equally, so "a hedge as good as the leg already
        held" is exactly be_open -- the completion price that matches the
        first leg's profit. Deriving it from straddle_first_leg_max_price
        instead, as this used to, tied the bar to the OPENER's ceiling: a leg
        filled at 0.40 would refuse a 0.40 hedge that matched it exactly and
        hold out for a 0.25 bearing no relation to what the round had cost.
        0.25 is a preferred entry price, not a gate on completing.
        """
        preferred = be_open
        limit = 1.0 - be_open
        if preferred >= limit:
            # be_open >= 0.5: the open leg was not cheap enough to be choosy
            # about the other. Anything that locks the round in will do.
            return False
        span = raw.duration_ms / 1000.0 - self._cfg.straddle_hedge_deadline_s
        slack = seconds_left - self._cfg.straddle_hedge_deadline_s
        urgency = (1.0 if span <= 0
                   else 1.0 - max(0.0, min(1.0, slack / span)))
        return be_other > preferred + urgency * (limit - preferred)

    def _open_first_leg(self, raw: Round, legs: dict[Side, float],
                        per_side: float, bankroll: float, mode: str,
                        now_ms: int, since_open: float) -> bool:
        """
        Open whichever side is cheap enough to stand on its own.

        UP and DOWN are interchangeable here and nothing prefers one to the
        other: the side that meets the price is the side that gets bought,
        and the round is completed later from the other end. A round whose
        DOWN goes cheap first is the same trade as one whose UP does.

        Cheap enough means straddle_first_leg_max_price -- 0.40 by default.
        That is not a preference dressed up as a rule: after filling at p,
        the other side stays worth buying all the way up to (1 - p), so a
        first leg at 0.40 leaves 0.60 of room to complete in, while one at
        0.49 leaves almost nothing. It is deliberately looser than the 0.25
        this strategy would prefer, because the opening window is the first
        minute of the round and 0.25 does not exist that early -- what the
        loosened price buys is the four minutes of completion time that make
        the hedge findable at all. Whatever it fills at is then the bar the
        second leg is measured against (_completion_is_worth_waiting_out).

        Returns True if a leg was opened.
        """
        # Whichever is cheaper, with no tie to UP or DOWN.
        side = min(legs, key=legs.get)
        price = legs[side]
        # Both ceilings bind. straddle_max_leg_price is the blunt "never pay
        # more than this for anything" limit; the first-leg ceiling is the
        # strategy's own, and is normally the tighter of the two.
        ceiling = min(self._cfg.straddle_first_leg_max_price,
                      self._cfg.straddle_max_leg_price)
        if not 0.0 < price <= ceiling:
            return False
        stake = min(per_side, self._available(bankroll))
        if stake < self._cfg.min_stake_usdt:
            return False

        quote = None
        if self._live:
            fresh = self._live_bankroll("straddle first leg")
            if fresh is None or fresh < stake:
                LOG.warning("Straddle %s: %.2f USDT needed to open the %s "
                            "leg but the wallet holds %s", raw.slug, stake,
                            side.value,
                            "an unreadable balance" if fresh is None
                            else "%.2f" % fresh)
                return False
            quote = self._client.get_quote(raw, side, stake)
            if quote.average_price > ceiling:
                LOG.info("Straddle %s: the %s quote at %.4f is dearer than "
                         "the %.2f first-leg ceiling the book suggested; "
                         "not opening", raw.slug, side.value,
                         quote.average_price, ceiling)
                return False
            price = quote.average_price

        self._watching.pop(raw.topic_id, None)
        self._seen[raw.topic_id] = raw.end_ms
        placed = self._place_leg(raw, side, price, stake, quote)
        if placed is None:
            return False              # killed by the venue; nothing opened
        price, stake, order_id = placed
        self._record_straddle_legs(raw, {side: (price, stake, order_id)},
                                   bankroll, mode, now_ms)
        LOG.info("STRADDLE LEG 1 %s | %s %.4f (%.2f) pays %.2f | now hunting "
                 "%s under %.4f to lock the round in (%.0fs since open)",
                 raw.slug, side.value, price, stake,
                 stake / breakeven_probability(price, raw.fee_bps),
                 side.other.value,
                 1.0 - breakeven_probability(price, raw.fee_bps),
                 since_open)
        return True

    def _complete_half_straddles(self, bankroll: float, mode: str,
                                 now_ms: int) -> None:
        """
        Buy the missing side of any round holding only one leg.

        This is the half of the strategy that makes it a strategy. The two
        sides of a live book sum to about 1.00, so a pair bought in one
        instant is nearly never profitable both ways -- but the sides move
        independently over the five minutes of a round, and a leg bought at
        0.25 early can be joined by the other side at 0.25 a minute later.
        Neither price was ever available alongside the other.

        Runs before any new round is opened, and is not subject to
        max_concurrent_positions: completing a hedge REMOVES exposure, and
        starving it of a slot in favour of opening fresh directional legs is
        exactly backwards.
        """
        for (symbol, side), pos in list(self._positions.items()):
            other = side.other
            if (symbol, other) in self._positions:
                continue
            raw = pos.rnd
            seconds_left = raw.seconds_remaining(now_ms)
            if seconds_left <= 0:
                continue

            price = self._book_price(raw, other)
            if price is None:
                # Late in a round the missing side is routinely quoted at
                # 0.00 or 1.00. Neither is completable; wait for a real one.
                LOG.debug("%s: no usable %s price to complete with yet",
                          raw.slug, other.value)
                continue
            budget = self._available(bankroll)
            stake, guaranteed = straddle_completion_stake(
                pos.committed_usdt, pos.signal.fill_price, price,
                raw.fee_bps, budget)
            past_deadline = seconds_left <= self._cfg.straddle_hedge_deadline_s

            if guaranteed:
                if self._completion_is_worth_waiting_out(
                        raw, breakeven_probability(pos.signal.fill_price,
                                                   raw.fee_bps),
                        breakeven_probability(price, raw.fee_bps),
                        seconds_left):
                    LOG.debug("%s: %s at %.4f would lock the round in, but "
                              "there is still time to want it cheaper",
                              raw.slug, other.value, price)
                    continue
            elif not past_deadline:
                LOG.debug("%s: %s at %.4f cannot cover the %.2f already on "
                          "%s (%.0fs left to find a price that can)",
                          raw.slug, other.value, price, pos.committed_usdt,
                          side.value, seconds_left)
                continue
            elif not self._cfg.straddle_force_hedge:
                continue
            else:
                LOG.warning(
                    "Straddle %s: no price for %s ever covered the %.2f on "
                    "%s, and the round closes in %.0fs. Hedging at %.4f "
                    "anyway -- the guaranteed profit is gone, but this is a "
                    "bounded loss instead of an all-or-nothing bet.",
                    raw.slug, other.value, pos.committed_usdt, side.value,
                    seconds_left, price)

            if stake < self._cfg.min_stake_usdt:
                LOG.debug("%s: completing %s needs %.2f, under the %.2f "
                          "venue minimum", raw.slug, other.value, stake,
                          self._cfg.min_stake_usdt)
                continue

            quote = None
            if self._live:
                fresh = self._live_bankroll("straddle completion")
                if fresh is None or fresh < stake:
                    LOG.warning("Straddle %s: %.2f USDT needed to complete "
                                "the %s side but the wallet holds %s",
                                raw.slug, stake, other.value,
                                "an unreadable balance" if fresh is None
                                else "%.2f" % fresh)
                    continue
                quote = self._client.get_quote(raw, other, stake)
                # Re-tested, NOT re-sized. place_order executes this quote's
                # id, which is bound to the size it was asked for, so the
                # only honest question is whether THIS trade still locks the
                # round in at the price it will actually fill at.
                if (straddle_worst_case_pnl(
                        pos.committed_usdt, stake, pos.signal.fill_price,
                        quote.average_price, raw.fee_bps) <= 0
                        and not (past_deadline
                                 and self._cfg.straddle_force_hedge)):
                    LOG.info("Straddle %s: the %s quote at %.4f no longer "
                             "covers the %.2f open on %s; still hunting",
                             raw.slug, other.value, quote.average_price,
                             pos.committed_usdt, side.value)
                    continue

            placed = self._place_leg(raw, other, price, stake, quote)
            if placed is None:
                # The hedge was killed, so the open leg is still open. Left
                # for the next poll rather than given up on.
                continue
            price, stake, order_id = placed
            self._record_straddle_legs(raw, {other: (price, stake, order_id)},
                                       bankroll, mode, now_ms)
            total = pos.committed_usdt + stake
            worst = straddle_worst_case_pnl(
                pos.committed_usdt, stake, pos.signal.fill_price, price,
                raw.fee_bps)
            LOG.info("STRADDLE COMPLETE %s | %s %.4f (%.2f) then %s %.4f "
                     "(%.2f) | either outcome pays %.2f on %.2f staked, "
                     "worst case %+.2f (%.0fs before close)",
                     raw.slug, side.value, pos.signal.fill_price,
                     pos.committed_usdt, other.value, price, stake,
                     total + worst, total, worst, seconds_left)

    def _record_straddle_legs(
            self, raw: Round,
            filled: dict[Side, tuple[float, float, str | None]],
            bankroll: float, mode: str, now_ms: int) -> None:
        """Journal every leg that actually reached the venue."""
        for side, (price, stake, order_id) in filled.items():
            # model_prob/edge are meaningless for a strategy with no
            # probability model; a neutral 0.5 keeps the journal schema and
            # calibration_report's bucketing arithmetic valid without
            # implying a directional forecast that was never made. edge is
            # left as a simple descriptive read of how far the fill sat from
            # a coin-flip breakeven -- not a signal this profile acted on.
            sig = Signal(side, model_prob=0.5, fill_price=price,
                         edge=0.5 - breakeven_probability(price, raw.fee_bps),
                         stake_usdt=stake,
                         seconds_left=raw.seconds_remaining(now_ms))
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id)
            self._positions[(raw.symbol, side)] = Position(
                tid, raw, sig, stake, 1)

    def _maybe_enter_straddle(self, bankroll: float, mode: str) -> None:
        """
        Buy both sides of a round -- usually at two different moments.

        No model probability and no side is picked. The one thing asked of a
        round is the payout test: whichever way it settles, the winning leg
        must return more than the pair cost together. That makes direction
        genuinely irrelevant, which is the only footing on which buying both
        sides makes sense.

        The catch is that a live book prices the two sides to sum to about
        1.00, so a pair bought in ONE instant almost never passes that test.
        Bought at two instants it can: the sides move independently over the
        five minutes of a round, so UP at 0.25 while spot slides and DOWN at
        0.25 after it bounces are both real prices that were simply never on
        offer at the same time. So there are two ways in:

          * both sides clear together right now -- taken immediately, sized
            by straddle_split, with no legging risk at all. Rare.
          * one side is cheap enough on its own
            (straddle_first_leg_max_price) -- opened alone, and completed
            later by _complete_half_straddles once the other side becomes
            worth buying, where "worth" means as good as the price the open
            leg itself got. That is the path this profile actually trades.

        Opening is confined to straddle_entry_window_s -- the first minute,
        so the rest of the round is completion time. Completing is not, and
        runs until straddle_hedge_deadline_s before settlement.

        Legs are separate MARKET FOK orders -- this venue has no limit order
        type -- so in live mode every leg is quoted and re-tested against the
        price that will actually execute before the order goes out.
        """
        now_ms = self._client.now_ms()
        self._prune(now_ms)
        # Before anything else, and deliberately outside the concurrency cap:
        # an open leg with no partner is the only directional exposure this
        # profile ever carries, and closing that gap beats opening new rounds
        # every time.
        self._complete_half_straddles(bankroll, mode, now_ms)

        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        available = self._available(bankroll)
        per_side = bankroll * self._cfg.straddle_stake_pct
        # Two different conditions, and conflating them is what hid the
        # original bug. Sizing below the venue minimum is a configuration
        # fault that will never clear on its own, so it is loud. Capital
        # already committed to another round is the ordinary state of a
        # profile holding positions, so it stays at debug.
        if per_side < self._cfg.min_stake_usdt:
            msg = ("Straddle cannot enter any round: %.2f USDT per leg "
                   "(%.1f%% of a %.2f bankroll) is below the %.2f venue "
                   "minimum. Raise straddle_stake_pct or fund the wallet -- "
                   "nothing will be traded until one of those changes."
                   % (per_side, self._cfg.straddle_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt))
            if msg != self._idle_reason:
                self._idle_reason = msg
                LOG.warning("%s", msg)
            return
        self._idle_reason = ""
        if available < per_side * 2:
            LOG.debug("No uncommitted bankroll for a straddle (%.2f needed, "
                      "%.2f available after the %.0f%% reserve and %.2f "
                      "already committed)", per_side * 2, available,
                      self._cfg.reserve_pct * 100, self._committed())
            return

        for raw in self._client.list_rounds():
            if raw.topic_id in self._seen:
                continue
            # A market already holding a leg belongs to
            # _complete_half_straddles, which ran above.
            if any(k[0] == raw.symbol for k in self._positions):
                continue

            since_open = (now_ms - raw.start_ms) / 1000.0
            if since_open < 0.0:
                continue                      # has not opened yet
            # Two bounds, and once either binds the round is finished for
            # opening purposes. The window caps how late a first leg may be
            # started; the runway floor refuses one that could never be
            # hedged -- completion stops at straddle_hedge_deadline_s and
            # needs time to work before then, so it is derived from that
            # rather than being another number to keep in sync.
            runway = self._cfg.straddle_hedge_deadline_s * 2.0
            if (since_open > self._cfg.straddle_entry_window_s
                    or raw.seconds_remaining(now_ms) <= runway):
                self._seen[raw.topic_id] = raw.end_ms
                continue

            priced = {side: self._book_price(raw, side)
                      for side in (Side.UP, Side.DOWN)}
            if any(price is None for price in priced.values()):
                LOG.debug("%s: no usable two-sided price (UP %s / DOWN %s)",
                          raw.slug, priced[Side.UP], priced[Side.DOWN])
                continue
            legs: dict[Side, float] = {side: price
                                       for side, price in priced.items()
                                       if price is not None}

            # Recomputed per round, not once per pass: entering one round
            # commits capital that the next round in the same pass must not
            # be sized against as though it were still free.
            free = self._available(bankroll)
            total = min(per_side * 2.0, free)
            stakes = dict(zip((Side.UP, Side.DOWN),
                              straddle_split(total, legs[Side.UP],
                                             legs[Side.DOWN], raw.fee_bps)))
            ok, worst, reason = self._straddle_payouts_clear(
                raw, legs, stakes, total)
            if not ok:
                LOG.debug("%s: %s (UP %.4f / DOWN %.4f, %.2f + %.2f staked)",
                          raw.slug, reason, legs[Side.UP], legs[Side.DOWN],
                          stakes[Side.UP], stakes[Side.DOWN])
                # The normal case, not a failure: the two sides of one book
                # sum to about 1.00. Take whichever side is cheap enough to
                # stand on its own and let the completion pass find the
                # other one at a price that never coexisted with this one.
                if self._open_first_leg(raw, legs, per_side, bankroll, mode,
                                        now_ms, since_open):
                    continue
                self._watching[raw.topic_id] = (raw.end_ms, reason)
                continue

            # Live pricing is a second opinion that can disagree with the
            # book: a quote average price carries the impact of THIS size,
            # which the top-of-book level does not. Both quotes are taken
            # BEFORE either order is placed -- quotes are non-binding and
            # place nothing -- and the gate is then re-tested against the
            # prices that will actually execute. Testing the book but
            # executing the quote is how a pair that cleared on paper turns
            # into a guaranteed loss in the account.
            quotes: dict[Side, Quote] = {}
            if self._live:
                fresh = self._live_bankroll("straddle entry")
                if fresh is None or fresh < total:
                    LOG.warning("Straddle %s: %.2f USDT needed for the pair "
                                "but the wallet holds %s; entering neither "
                                "side", raw.slug, total,
                                "an unreadable balance" if fresh is None
                                else "%.2f" % fresh)
                    continue
                for side in (Side.UP, Side.DOWN):
                    quotes[side] = self._client.get_quote(raw, side,
                                                          stakes[side])
                quoted = {side: q.average_price for side, q in quotes.items()}
                ok, worst, reason = self._straddle_payouts_clear(
                    raw, quoted, stakes, total)
                if not ok:
                    LOG.info("Straddle %s: %s once quoted (UP %.4f / DOWN "
                             "%.4f against a book of %.4f / %.4f); entering "
                             "neither side", raw.slug, reason,
                             quoted[Side.UP], quoted[Side.DOWN],
                             legs[Side.UP], legs[Side.DOWN])
                    self._watching[raw.topic_id] = (raw.end_ms, reason)
                    continue
                legs = quoted

            self._watching.pop(raw.topic_id, None)
            self._seen[raw.topic_id] = raw.end_ms

            # try/finally, not a plain loop: once the first order is placed
            # that money is committed whatever happens to the second, and an
            # earlier version discarded the whole filled map on any abort --
            # leaving a real, unhedged live position that was never
            # journalled, never settled and never claimed. Anything that
            # filled gets recorded before the failure is allowed to surface.
            filled: dict[Side, tuple[float, float, str | None]] = {}
            try:
                for side in (Side.UP, Side.DOWN):
                    placed = self._place_leg(raw, side, legs[side],
                                             stakes[side],
                                             quotes.get(side))
                    if placed is None:
                        continue     # killed by the venue; no position
                    filled[side] = placed
            finally:
                # Inside the finally, so a leg that reached the venue is
                # journalled even when the exception from the other one is
                # about to unwind this whole call.
                self._record_straddle_legs(raw, filled, bankroll, mode,
                                           now_ms)
                if len(filled) == 1:
                    only = next(iter(filled))
                    LOG.error("Straddle %s: only the %s leg opened. This "
                              "round is now a one-sided directional bet, "
                              "not a hedge, and the payout gate no longer "
                              "holds.", raw.slug, only.value)

            if len(filled) == 2:
                LOG.info("STRADDLE %s | UP %.4f (%.2f) / DOWN %.4f (%.2f) | "
                         "either outcome pays %.2f on %.2f staked, worst "
                         "case %+.2f (%.0fs since open)",
                         raw.slug, legs[Side.UP], filled[Side.UP][1],
                         legs[Side.DOWN], filled[Side.DOWN][1],
                         total + worst, total, worst, since_open)

            if len(self._positions) >= self._cfg.max_concurrent_positions:
                return

    def _maybe_enter_model(self, bankroll: float, mode: str) -> None:
        # The cap the config actually declares. An earlier version returned
        # whenever ANY position was open, which made
        # max_concurrent_positions dead: its default of 2 could never be
        # reached, every multi-market deployment silently traded one market
        # at a time, and the setting read as configuration while behaving as
        # a constant.
        #
        # What that guard was really protecting is narrower and is enforced
        # per market below: never open a SECOND position on a symbol that
        # already has one. That is the same bet twice, not diversification,
        # and it would orphan the first in the journal.
        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        now_ms = self._client.now_ms()
        self._prune(now_ms)

        available = self._available(bankroll)
        if available < self._cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a new position "
                      "(%.2f committed of %.2f)", self._committed(), bankroll)
            return

        for raw in self._client.list_rounds():
            if raw.topic_id in self._seen:
                continue
            # One position per market: a second on the same symbol would be
            # the same bet twice, not diversification.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue
            # The widest window any trend could open. The exact one depends on
            # the trend for THIS market's settlement feed, which is not known
            # until the round is hydrated -- so screen loosely here and let
            # evaluate() apply the real bound. Screening tightly would discard
            # precisely the early rounds the trend exists to catch.
            if not (self._cfg.entry_window_end_s
                    <= raw.seconds_remaining(now_ms)
                    <= entry_window_start_s(self._cfg,
                                            self._cfg.trend_follow)):
                continue

            rnd = self._hydrated.get(raw.topic_id) or self._client.hydrate(raw)
            if rnd is None:
                LOG.debug("No strike yet for %s", raw.slug)
                continue
            self._hydrated[raw.topic_id] = rnd

            if self._cfg.min_liquidity > 0:
                if rnd.liquidity is None:
                    LOG.debug("Skipping %s: liquidity unknown and a minimum "
                              "is configured", rnd.slug)
                    continue
                if rnd.liquidity < self._cfg.min_liquidity:
                    LOG.debug("Skipping %s: liquidity %.0f below %.0f",
                              rnd.slug, rnd.liquidity, self._cfg.min_liquidity)
                    continue

            # Spot and volatility must come from the same series, or the
            # model is fed a price and a sigma describing different assets.
            symbol = self._client.market_symbol(rnd.feed_symbol)
            spot = self._client.spot_price(symbol)
            sigma = self._vol.sigma_annual(symbol)
            if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
                LOG.warning("Skipping %s: volatility clamped, so every edge "
                            "estimate would be unreliable", rnd.slug)
                continue
            tail_df = self._vol.tail_df(symbol)
            trend = self._vol.trend(symbol)
            book = {}
            for side in Side:
                levels = self._client.asks_for(rnd, side)
                if levels:
                    book[side] = levels

            # Size against uncommitted funds, never the full balance.
            verdict = assess(rnd, spot, sigma, available, now_ms, self._cfg,
                             book or None, tail_df, trend)
            if verdict.signal is None:
                # Deliberately NOT marked as seen. The round stays under
                # review for as long as it is live, because the price that
                # was too expensive a moment ago may not be in ten seconds --
                # writing a round off on its first look is how a return floor
                # turns into a bot that never trades.
                self._watching[rnd.topic_id] = (rnd.end_ms, verdict.blocked_by)
                LOG.debug("%s: %s (%.0fs left)", rnd.slug,
                          verdict.blocked_by, rnd.seconds_remaining(now_ms))
                continue
            sig = verdict.signal
            self._watching.pop(rnd.topic_id, None)

            if self._cfg.scale_in:
                # Open with a fraction of the target so there is room to add
                # if the round keeps going our way.
                first = max(sig.stake_usdt * self._cfg.scale_in_initial_pct,
                            self._cfg.min_stake_usdt)
                sig = replace(sig, stake_usdt=min(first, sig.stake_usdt))

            order_id = None
            if self._live:
                # Re-read the balance immediately before committing. The
                # figure from the top of the loop is seconds old and may
                # predate a settlement, a redemption landing, or a manual
                # withdrawal -- sizing from it can request more than the
                # account holds, which the venue rejects with -9000.
                fresh = self._live_bankroll("entry")
                if fresh is None:
                    continue
                if fresh < bankroll:
                    sig = self._resize(sig, rnd, fresh)
                    if sig is None:
                        continue
                if sig.stake_usdt > fresh:
                    LOG.warning("Stake %.2f exceeds the live balance %.2f; "
                                "skipping", sig.stake_usdt, fresh)
                    continue
                quote = self._client.get_quote(rnd, sig.side, sig.stake_usdt)

                # The quote is authoritative. Re-apply every price filter to it
                # and walk away if the venue prices worse than our screen
                # assumed -- the ceiling must bind on the executed price, not
                # merely on the order book we looked at a moment earlier.
                if quote.average_price > self._cfg.max_entry_price:
                    LOG.info("Quote %.4f above price ceiling %.2f; skipping",
                             quote.average_price, self._cfg.max_entry_price)
                    continue
                if not clears_return(quote.average_price, rnd.fee_bps,
                                     self._cfg):
                    LOG.info("Quote %.4f returns %.1f%% on a win, under the "
                             "%.0f%% floor; skipping",
                             quote.average_price,
                             win_return(quote.average_price, rnd.fee_bps) * 100,
                             self._cfg.min_win_return * 100)
                    continue
                if not clears_edge(sig.model_prob, quote.average_price,
                                   self._cfg, rnd.fee_bps):
                    LOG.info("Quote worse than screen (%.3f vs %.3f); skipping",
                             quote.average_price, sig.fill_price)
                    continue
                edge = (sig.model_prob
                        - breakeven_probability(quote.average_price,
                                                rnd.fee_bps))
                if abs(quote.price_impact) > self._cfg.max_price_impact:
                    LOG.info("Price impact %.1f%% too high; skipping",
                             quote.price_impact * 100)
                    continue

                order_id = self._client.place_order(rnd, quote,
                                                    sig.stake_usdt)
                # The order id alone proves nothing: PlaceOrderResponse has no
                # fill information, and a FOK order that cannot fill is killed
                # while still returning an id. Recording a position on that id
                # invents a trade, which then "settles" and books a profit
                # that was never made.
                if self._cfg.confirm_fills:
                    try:
                        filled = self._client.confirm_fill(order_id,
                                                           sig.stake_usdt)
                    except (ApiError, requests.RequestException) as exc:
                        LOG.error("NOT recording a position for %s: %s",
                                  rnd.slug, exc)
                        self._seen[rnd.topic_id] = rnd.end_ms
                        continue
                    if abs(filled - sig.stake_usdt) > EPS:
                        LOG.warning("Filled %.4f of %.4f requested on %s; "
                                    "tracking the filled amount",
                                    filled, sig.stake_usdt, rnd.slug)
                        sig = replace(sig, stake_usdt=filled)
                if quote.fee_usdt > 0:
                    LOG.info("Venue fee %.4f USDT (%.0f bps of stake)",
                             quote.fee_usdt,
                             quote.fee_usdt / sig.stake_usdt * 10_000)
                sig = replace(sig, fill_price=quote.average_price, edge=edge)
                LOG.info("Order %s filled at %.4f for %.4f shares", order_id,
                         quote.average_price, quote.amount_out_shares)

            mult = kelly_multiple(sig.stake_usdt, bankroll, sig.model_prob,
                                  sig.fill_price, rnd.fee_bps)
            mult_s = "" if mult is None else f" [{mult:.2f}x Kelly]"
            if mult is not None and mult > 1.0:
                LOG.warning("Staking %.2fx the full-Kelly fraction because the "
                            "venue minimum exceeds the Kelly size on a %.2f "
                            "bankroll", mult, bankroll)
            LOG.info("ENTER %s %s | fill %.3f model %.3f edge %+.3f "
                     "pays %+.0f%% stake %.2f%s (%.0fs left)%s",
                     rnd.slug, sig.side.value,
                     sig.fill_price, sig.model_prob, sig.edge,
                     win_return(sig.fill_price, rnd.fee_bps) * 100,
                     sig.stake_usdt, mult_s, sig.seconds_left,
                     f"  TREND {trend.describe()}" if sig.trend_boosted
                     else "")

            tid = self._journal.record(mode, rnd, sig, spot, sigma, bankroll,
                                       order_id)
            self._seen[rnd.topic_id] = rnd.end_ms
            self._positions[(rnd.symbol, sig.side)] = Position(
                tid, rnd, sig, sig.stake_usdt, 1)
            available -= sig.stake_usdt
            if (len(self._positions) >= self._cfg.max_concurrent_positions
                    or available < self._cfg.min_stake_usdt):
                return

    def _maybe_enter_last_minute(self, bankroll: float, mode: str) -> None:
        """
        Buy whichever side the book has already picked, as the clock runs out.

        One rule, and nothing underneath it. No model probability, no edge
        test, no buffer, no trend, no volatility -- none of it is computed,
        let alone consulted. With last_minute_start_s left in the round, read
        the two asks, take the DEARER one, and stop.

        The floor and the fallback are one rule in two halves, not a rule and
        an excuse:

          * above last_minute_fallback_s the leader must show
            last_minute_price_floor. A leader under it means the round is
            still a genuine contest, and there is time left for it to stop
            being one, so nothing is bought yet.
          * at or below last_minute_fallback_s that time has run out, and
            "no side reached 0.75" has itself become the answer: the round
            IS close, the leader is the best read anyone has of how it will
            land, and -- because it failed the floor -- it is cheap. So it
            is bought at whatever it costs.

        The dear end is where this can bleed, and the dear end is the FIRST
        branch, not the second. By default nothing caps the price above the
        floor, so a round already decided at 55 seconds quotes 0.97 and gets
        bought for about 3% on a win, where it takes 32 wins to cover one
        loss -- against 3 wins at the 0.75 floor. That is the profile as
        specified, and last_minute_max_price is the one knob that changes it:
        set it to 0.90 or 0.85 and those rounds are refused instead. It is
        left at 1.0 by default because refusing them is a decision about
        which trades the strategy is for, not a bug fix. The
        favourite-longshot table in --calibration-report is what says whether
        the venue's late favourites win often enough to pay for them.

        Two rounds are left alone, and neither is a judgement about price:
        one where the sides are quoted level, because there is no dearer side
        to buy and resolving that with a coin flip on UP would be inventing a
        signal; and one where the leader has rounded to 1.00, because a
        contract at 1.00 cannot pay back more than it cost, so buying it is
        a fee with extra steps.

        Everything the other strategies share still applies: the daily loss
        limit, the calibration breaker, the streak cap, the reserve, the
        concurrency cap and fill confirmation. Those are not strategy, they
        are the difference between a bot that is losing and one that has
        stopped.
        """
        now_ms = self._client.now_ms()
        self._prune(now_ms)

        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        target = bankroll * self._cfg.last_minute_stake_pct
        # Two different conditions, and conflating them is what once hid the
        # same bug on the straddle path. Sizing under the venue minimum is a
        # configuration fault that will never clear on its own, so it is loud
        # and said once. Capital tied up in another market is the ordinary
        # state of a profile holding a position, so it stays quiet.
        if target < self._cfg.min_stake_usdt:
            msg = ("The last-minute profile cannot enter any round: %.2f "
                   "USDT (%.1f%% of a %.2f bankroll) is below the %.2f venue "
                   "minimum. Raise last_minute_stake_pct or fund the wallet "
                   "-- nothing will be traded until one of those changes."
                   % (target, self._cfg.last_minute_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt))
            if msg != self._idle_reason:
                self._idle_reason = msg
                LOG.warning("%s", msg)
            return
        self._idle_reason = ""

        if self._available(bankroll) < self._cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a last-minute entry "
                      "(%.2f committed of %.2f, %.0f%% reserved)",
                      self._committed(), bankroll,
                      self._cfg.reserve_pct * 100)
            return

        for raw in self._client.list_rounds():
            if raw.topic_id in self._seen:
                continue
            # One position per market: a second on the same symbol is the
            # same bet twice, not diversification.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue

            secs = raw.seconds_remaining(now_ms)
            if secs > self._cfg.last_minute_start_s:
                # Deliberately NOT marked seen. Its minute has not come yet.
                continue
            if secs <= self._cfg.last_minute_deadline_s:
                # Out of time. Whatever reason was last recorded against this
                # round is the informative one, so it is left standing rather
                # than overwritten with the clock running out -- that is a
                # consequence of the real reason, not the reason.
                self._watching.setdefault(
                    raw.topic_id,
                    (raw.end_ms,
                     "the last minute ran out with no side to buy"))
                self._seen[raw.topic_id] = raw.end_ms
                continue

            asks = {side: self._raw_book_price(raw, side)
                    for side in (Side.UP, Side.DOWN)}
            if asks[Side.UP] == asks[Side.DOWN]:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the two sides are priced level")
                continue
            side = max(asks, key=asks.get)
            price = self._book_price(raw, side)
            if price is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the leading side is priced at 1.00")
                continue
            if price > self._cfg.last_minute_max_price:
                # Above the ceiling there is too little left to win for the
                # whole stake it risks. Unlike the floor this is never
                # relaxed by the clock: a round that is already decided does
                # not become a better bet for being nearly over.
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the leading side is priced above the ceiling")
                continue
            if (price < self._cfg.last_minute_price_floor
                    and secs > self._cfg.last_minute_fallback_s):
                self._watching[raw.topic_id] = (
                    raw.end_ms, "no side has reached the price floor")
                continue

            # Recomputed per round, not once per pass: entering one round
            # commits capital that the next round in this pass must not be
            # sized against as though it were still free.
            stake = min(target, self._available(bankroll))
            if stake < self._cfg.min_stake_usdt:
                LOG.debug("%s: %.2f left after the reserve and %.2f already "
                          "committed, under the %.2f minimum", raw.slug,
                          stake, self._committed(), self._cfg.min_stake_usdt)
                continue

            quote = None
            if self._live:
                # Re-read the balance immediately before committing. The
                # figure from the top of the loop is seconds old and may
                # predate a settlement, a redemption landing or a manual
                # withdrawal; sizing from it can ask for more than the
                # account holds, which the venue rejects with -9000.
                fresh = self._live_bankroll("last-minute entry")
                if fresh is None:
                    continue
                stake = min(stake, fresh)
                if stake < self._cfg.min_stake_usdt:
                    LOG.warning("%s: the wallet holds %.2f, under the %.2f "
                                "minimum order; skipping", raw.slug, fresh,
                                self._cfg.min_stake_usdt)
                    continue
                quote = self._client.get_quote(raw, side, stake)
                # The quote is authoritative and the book was only a screen.
                # Re-test the rule on the price that will actually execute,
                # or the floor binds on a number nobody pays.
                if not 0.0 < quote.average_price < 1.0:
                    LOG.info("%s: the %s quote came back at %.4f, which has "
                             "no payout; skipping", raw.slug, side.value,
                             quote.average_price)
                    continue
                if quote.average_price > self._cfg.last_minute_max_price:
                    LOG.info("%s: the %s quote at %.4f is above the %.2f "
                             "ceiling the book suggested it would clear; "
                             "skipping", raw.slug, side.value,
                             quote.average_price,
                             self._cfg.last_minute_max_price)
                    self._watching[raw.topic_id] = (
                        raw.end_ms,
                        "the leading side is priced above the ceiling")
                    continue
                if (quote.average_price < self._cfg.last_minute_price_floor
                        and secs > self._cfg.last_minute_fallback_s):
                    LOG.info("%s: the %s quote at %.4f is under the %.2f "
                             "floor with %.0fs left; waiting", raw.slug,
                             side.value, quote.average_price,
                             self._cfg.last_minute_price_floor, secs)
                    self._watching[raw.topic_id] = (
                        raw.end_ms, "no side has reached the price floor")
                    continue
                price = quote.average_price

            self._watching.pop(raw.topic_id, None)
            self._seen[raw.topic_id] = raw.end_ms
            placed = self._place_leg(raw, side, price, stake, quote)
            if placed is None:
                continue              # killed by the venue; nothing opened
            price, stake, order_id = placed

            # model_prob is the MARKET'S implied probability, not a forecast
            # of ours -- this strategy makes none. Recording the price
            # restated as a probability is what makes the calibration breaker
            # mean something here: it then asks "are the favourites I am
            # buying winning as often as I paid for them to?", which is the
            # one health question this profile has, and halts if they are
            # not. A neutral 0.5 would have left that test permanently and
            # uninformatively positive. edge is 0.0 for the same reason:
            # paying the market price is by definition no edge over it.
            implied = breakeven_probability(price, raw.fee_bps)
            sig = Signal(side, model_prob=implied, fill_price=price,
                         edge=0.0, stake_usdt=stake,
                         seconds_left=raw.seconds_remaining(now_ms))
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id)
            self._positions[(raw.symbol, side)] = Position(
                tid, raw, sig, stake, 1)
            LOG.info("LAST MINUTE %s | %s %.4f (%.2f) implied %.1f%% "
                     "pays %+.0f%% (%.0fs left)%s", raw.slug, side.value,
                     price, stake, implied * 100,
                     win_return(price, raw.fee_bps) * 100, secs,
                     "  [floor dropped]"
                     if price < self._cfg.last_minute_price_floor else "")

            if (len(self._positions) >= self._cfg.max_concurrent_positions
                    or self._available(bankroll) < self._cfg.min_stake_usdt):
                return

    def _maybe_scale_in_all(self, bankroll: float) -> None:
        if self._cfg.last_minute:
            # One order, one round, held to settlement. Config rejects
            # last_minute + scale_in outright; this is the belt to that
            # brace, so a future caller cannot route around the validation.
            return
        if self._cfg.straddle:
            # Both legs are bought once, at round-open, and left alone until
            # settlement -- no top-up, no exit, nothing sold mid-round. This
            # profile has no model probability for scale-in to top up
            # toward, which is also why straddle+scale_in cannot both be
            # enabled (see Config.__post_init__).
            return
        for key in list(self._positions):
            self._maybe_scale_in(bankroll, key)

    def _maybe_scale_in(self, bankroll: float,
                        key: tuple[str, Side] | None = None) -> None:
        """
        Top up an open position as the round moves further into our favour.

        The target is the Kelly stake for the CURRENT probability. If the
        buffer has grown, the target grows, and we add the difference. If the
        round has turned against us the target falls and we add nothing --
        adding there would be chasing a loser, which is the failure this
        deliberately avoids.

        Because the target is recomputed rather than accumulated, total
        exposure to one round stays bounded by Kelly no matter how many
        tranches are added.
        """
        pos = (self._positions.get(key) if key is not None
               else self._position)
        if pos is None or not self._cfg.scale_in:
            return
        key = (pos.rnd.symbol, pos.signal.side)
        secs = pos.rnd.seconds_remaining(self._client.now_ms())
        if secs <= self._cfg.entry_window_end_s:
            return                       # too late to fill

        symbol = self._client.market_symbol(pos.rnd.feed_symbol)
        spot = self._client.spot_price(symbol)
        sigma = self._vol.sigma_annual(symbol)
        if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
            return
        tail_df = self._vol.tail_df(symbol)

        if pos.rnd.strike is None:
            return
        p_up = digital_up_probability(spot, pos.rnd.strike, sigma, secs, tail_df)
        prob = p_up if pos.signal.side is Side.UP else 1.0 - p_up
        if prob <= pos.signal.model_prob:
            return                       # not more favourable than before

        levels = self._client.asks_for(pos.rnd, pos.signal.side)
        if not levels:
            return
        price = levels[0][0]
        if not (self._cfg.min_entry_price <= price <= self._cfg.max_entry_price):
            return
        if not clears_return(price, pos.rnd.fee_bps, self._cfg):
            return
        if not clears_edge(prob, price, self._cfg, pos.rnd.fee_bps):
            return

        target = kelly_stake(bankroll + pos.committed_usdt, prob, price,
                             self._cfg, pos.rnd.fee_bps)
        if pos.signal.trend_boosted:
            # The position was opened at trend size; topping up to the plain
            # Kelly target would shrink it back mid-round, which is neither
            # the trend rule nor the Kelly rule but an accident of applying
            # one at entry and the other afterwards.
            target = boosted_stake(target, bankroll + pos.committed_usdt,
                                   prob, price, self._cfg, pos.rnd.fee_bps)
        topup = target - pos.committed_usdt
        floor = max(self._cfg.scale_in_min_topup, self._cfg.min_stake_usdt)
        if topup < floor:
            return

        # Trim so the blended fill stays under the ceiling. Without this a
        # top-up at a high price silently converts a position that needed six
        # wins per loss into one needing fifteen.
        cap = blended_price_cap(self._cfg, pos.rnd.fee_bps)
        allowed = max_topup_within_blend(
            pos.committed_usdt, pos.signal.fill_price, price, cap)
        if allowed < floor:
            LOG.debug("No top-up for %s: blended price would exceed %.3f",
                      pos.rnd.slug, cap)
            return
        if topup > allowed:
            LOG.info("Trimming top-up %.2f -> %.2f to hold the blended price "
                     "under %.3f", topup, allowed, cap)
            topup = allowed
        avg = walk_book(levels, topup)
        if avg is None or not clears_edge(prob, avg, self._cfg, pos.rnd.fee_bps):
            return
        if not clears_return(avg, pos.rnd.fee_bps, self._cfg):
            return

        if self._live:
            fresh = self._live_bankroll("scale-in")
            if fresh is None or topup > fresh:
                LOG.info("Skipping top-up: %.2f needed, %.2f available",
                         topup, fresh if fresh is not None else -1.0)
                return
            quote = self._client.get_quote(pos.rnd, pos.signal.side, topup)
            if quote.average_price > self._cfg.max_entry_price:
                return
            if not clears_return(quote.average_price, pos.rnd.fee_bps,
                                 self._cfg):
                return
            if pos.average_price(topup, quote.average_price) > cap + EPS:
                # The trim above was computed against the book; the venue's
                # executable price can be worse, and the blend is what pays.
                LOG.info("Top-up quote %.4f would blend past %.3f; skipping",
                         quote.average_price, cap)
                return
            if not clears_edge(prob, quote.average_price, self._cfg,
                               pos.rnd.fee_bps):
                return
            if abs(quote.price_impact) > self._cfg.max_price_impact:
                return
            topup_order = self._client.place_order(pos.rnd, quote, topup)
            if self._cfg.confirm_fills:
                try:
                    filled = self._client.confirm_fill(topup_order, topup)
                except (ApiError, requests.RequestException) as exc:
                    # The opener stands; only the top-up failed. Adding it to
                    # the position would overstate exposure on a fill that
                    # never happened.
                    LOG.error("Top-up on %s not confirmed, leaving the "
                              "position unchanged: %s", pos.rnd.slug, exc)
                    return
                if abs(filled - topup) > EPS:
                    LOG.warning("Top-up filled %.4f of %.4f on %s",
                                filled, topup, pos.rnd.slug)
                    topup = filled
            if quote.fee_usdt > 0:
                LOG.debug("Top-up fee %.4f USDT on %.2f staked",
                          quote.fee_usdt, topup)
            avg = quote.average_price

        blended = pos.average_price(topup, avg)
        LOG.info("SCALE-IN %s +%.2f at %.3f (prob %.3f, %.0fs left) -> "
                 "committed %.2f, blended %.3f, %.1f wins per loss",
                 pos.rnd.slug, topup, avg, prob, secs,
                 pos.committed_usdt + topup, blended, wins_per_loss(blended))

        self._positions[key] = replace(
            pos,
            signal=replace(pos.signal, model_prob=prob, fill_price=blended,
                           stake_usdt=pos.committed_usdt + topup),
            committed_usdt=pos.committed_usdt + topup,
            tranches=pos.tranches + 1)

    def _live_bankroll(self, context: str) -> float | None:
        """Freshly read tradable balance, or None if it cannot be read."""
        try:
            return self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.warning("Could not confirm the balance before %s: %s",
                        context, exc)
            return None

    def _resize(self, sig: Signal, rnd: Round,
                bankroll: float) -> Signal | None:
        """Re-derive the stake against a balance that has since changed."""
        stake = kelly_stake(bankroll, sig.model_prob, sig.fill_price,
                            self._cfg, rnd.fee_bps)
        if stake <= 0:
            LOG.info("Balance fell to %.2f; no stake clears the limits now",
                     bankroll)
            return None
        if sig.trend_boosted:
            stake = boosted_stake(stake, bankroll, sig.model_prob,
                                  sig.fill_price, self._cfg, rnd.fee_bps)
        if self._cfg.scale_in:
            first = max(stake * self._cfg.scale_in_initial_pct,
                        self._cfg.min_stake_usdt)
            stake = min(first, stake)
        if abs(stake - sig.stake_usdt) > EPS:
            LOG.info("Resized %.2f -> %.2f against a live balance of %.2f",
                     sig.stake_usdt, stake, bankroll)
        return replace(sig, stake_usdt=stake)

    def _settle_open(self) -> None:
        for key in list(self._positions):
            self._settle_one(key)

    def _settle_one(self, key: tuple[str, Side]) -> None:
        pos = self._positions.get(key)
        if pos is None:
            return
        now_ms = self._client.now_ms()
        if now_ms < pos.rnd.end_ms + self._cfg.settle_grace_s * 1000:
            return

        winner: Side | None = None
        pnl: float | None = None
        source = "venue"

        settled = self._client.settled_outcome(pos.rnd)
        if settled is not None:
            winner, venue_pnl = settled
            if self._live and venue_pnl is not None:
                pnl = venue_pnl
        else:
            final = self._client.final_price(pos.rnd)
            if final is not None and pos.rnd.strike is not None:
                if final == pos.rnd.strike:
                    # Unverified tie rule: do not guess on a real position.
                    LOG.warning("%s closed exactly at the strike (%.8f); the "
                                "venue's tie rule is unknown, so this is left "
                                "for venue settlement rather than guessed",
                                pos.rnd.slug, final)
                    return
                winner = Side.UP if final > pos.rnd.strike else Side.DOWN
                source = "endPrice"

        if winner is None:
            if now_ms > pos.rnd.end_ms + self._cfg.settle_timeout_s * 1000:
                LOG.error("Cannot settle %s; left unresolved in journal",
                          pos.rnd.slug)
                # Only THIS leg, not every open position -- a straddle round
                # can have one leg settle cleanly while the other's outcome
                # lookup is still stuck, and clearing both would abandon a
                # leg that was never actually resolved.
                self._positions.pop(key, None)
            return

        won = winner is pos.signal.side
        if pnl is None:
            pnl = settle_pnl(max(pos.committed_usdt, pos.signal.stake_usdt),
                             pos.signal.fill_price, won, pos.rnd.fee_bps)
        if not self._live:
            self._paper_bankroll += pnl

        # Read the balance before claiming so the change can be reconciled
        # against what we expected, rather than inferred.
        before: float | None = None
        if self._live:
            before = self._live_bankroll("settlement")

        if won and self._live:
            # Say what is owed before handing it to the claim worker. The
            # credit lands minutes later and may land light; declaring it is
            # what lets reconciliation read a shortfall as this trade's
            # result rather than as a stranger's withdrawal.
            payout = max(pos.committed_usdt, pos.signal.stake_usdt) / max(
                pos.signal.fill_price, EPS)
            self._risk_for(key[0]).expect_credit(payout)
            if self._account_risk is not None:
                self._account_risk.expect_credit(payout)
            self._claim(pos)

        self._journal.resolve(pos.trade_id, won, pnl, source)
        self._risk_for(key[0]).record_result(won, pos.signal.model_prob, pnl)
        if self._account_risk is not None:
            self._account_risk.record_result(won, pos.signal.model_prob, pnl)
        self._positions.pop(key, None)
        after = self._bankroll()
        LOG.info("SETTLED %s -> %s  P&L %+.2f  bankroll %.2f  [%s]",
                 pos.rnd.slug, "WIN" if won else "LOSS", pnl, after, source)
        if before is not None:
            self._reconcile(pos, won, pnl, before, after)

        self._settled_count += 1
        if (self._cfg.report_every
                and self._settled_count % self._cfg.report_every == 0):
            for line in self._journal.calibration_report(
                    self._cfg.profile_name).split("\n"):
                LOG.info("| %s", line)

    def _reconcile(self, pos: Position, won: bool, expected_pnl: float,
                   before: float, after: float) -> None:
        """
        Compare the actual balance change against what settling should move.

        The bot's own arithmetic and the venue's accounting should agree. When
        they do not, the venue is right and something here is wrong -- a fee we
        did not model, a partial fill, or a figure counted twice. Reporting the
        gap turns a silent drift into a visible one.

        WHAT SETTLING IS ACTUALLY EXPECTED TO MOVE
        -----------------------------------------
        Not the P&L. The stake left the balance at ENTRY, so a losing round
        moves the balance by nothing at all when it settles -- there is
        nothing left to lose. Comparing the balance delta against a P&L of
        -stake made every single loss report a mismatch the size of the whole
        stake, complete with a hint that something was double-counting it.
        Nothing was; the two numbers were simply measuring different events.

        A win moves the balance by the GROSS payout, and only once the claim
        has been credited on chain -- which is normally after this runs, so
        that case is noted rather than flagged.

        WHY THE GAP IS BOOKED AND NOT JUST PRINTED
        ------------------------------------------
        This used to warn and stop there, which left the bot certain that its
        own arithmetic was wrong and unwilling to do anything about it. The
        residue did not disappear: RiskManager met it later, could not tell
        it from a deposit, and rebased it into the day's baseline. So a fee
        the model under-counted by a few cents was laundered once per round,
        every round, and the daily loss limit drifted further from the
        account it exists to protect for as long as the bot ran.

        Here the measurement is trustworthy on its own terms. Both balances
        are read seconds apart around a single settlement -- a window far too
        narrow for a deposit to be the likely explanation -- so the gap is
        charged to PnL, where the venue's version wins. Anything genuinely
        external is still caught later, by the reconciliation that is built
        to look for it.
        """
        actual = after - before
        # A loss should move nothing: the money went out when the order did.
        expected_move = (pos.committed_usdt / max(pos.signal.fill_price, EPS)
                         if won else 0.0)
        reference = max(abs(expected_move), self._cfg.min_stake_usdt)
        drift = abs(actual - expected_move)

        if won and self._unredeemed and abs(actual) < EPS:
            LOG.debug("Balance unchanged; winnings still unredeemed")
            return
        if drift <= reference * self._cfg.reconcile_tolerance:
            LOG.debug("Reconciled %s: expected the balance to move %+.4f, "
                      "it moved %+.4f (P&L %+.4f)",
                      "a win" if won else "a loss", expected_move, actual,
                      expected_pnl)
            return

        correction = actual - expected_move
        self._correct_pnl(pos.rnd.symbol, correction)
        LOG.warning(
            "RECONCILE MISMATCH on %s: settling %s should have moved the "
            "balance %+.4f, it moved %+.4f (gap %.4f, P&L %+.4f). The venue "
            "is authoritative -- something here is counting a stake or a fee "
            "the venue does not, so %+.4f is booked against today's P&L.",
            pos.rnd.slug, "a win" if won else "a loss", expected_move, actual,
            drift, expected_pnl, correction)

    def _drain(self, timeout_s: float | None = None) -> None:
        deadline = time.time() + (timeout_s if timeout_s is not None
                                  else self._cfg.drain_timeout_s)
        while self._positions and time.time() < deadline:
            try:
                self._settle_open()
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("Settle retry: %s", exc)
            time.sleep(self._cfg.drain_poll_s)


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


# Three services rather than one: any of them can be down, rate-limited or
# blocked, and "could not determine the IP" is a much worse answer here than
# a slightly slower one.
IP_SERVICES = ("https://api.ipify.org?format=json",
               "https://ifconfig.me/all.json",
               "https://ipinfo.io/json")


def outbound_ip(session: requests.Session,
                timeout: float = 8.0) -> str | None:
    """The address this process appears to come from, or None."""
    for url in IP_SERVICES:
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code != 200:
                continue
            body = response.json()
            found = body.get("ip") or body.get("ip_addr")
            if found:
                return str(found)
        except (requests.RequestException, ValueError) as exc:
            # Not silent: three services are tried precisely because any one
            # can be down, but "all three failed" is a real finding and has
            # to be visible under --verbose rather than reported as "no IP".
            LOG.debug("IP lookup via %s failed: %s", url, exc)
    return None


def wait_for_auth(cfg: Config, client: PredictionClient) -> bool:
    """
    Knock on a signed, IP-gated endpoint until Binance accepts us.

    WHY WAITING IS THE RIGHT ANSWER HERE
    ------------------------------------
    Binance's key allowlist accepts individual addresses. On shared egress
    the address is not known until the process is already running, and it can
    change on any restart -- so it cannot be added to the allowlist in
    advance. Failing on the first refusal means the deploy dies within a
    second of printing the one piece of information needed to fix it.

    Retrying converts that race into a window: the address is on screen, and
    the process keeps trying until the allowlist entry lands.

    Only AUTH refusals and network errors are retried. A 451 is the server's
    region, not its address, and no amount of waiting changes which continent
    the worker is on; anything else is a real fault and is reported at once
    rather than hidden behind several minutes of silence.
    """
    deadline = time.time() + cfg.auth_wait_timeout_s
    started = time.time()
    attempt = 0

    while True:
        attempt += 1
        try:
            client.sync_clock()
            client.wallet()
            if attempt > 1:
                print(f"  accepted after {time.time() - started:.0f}s "
                      f"({attempt} attempts).\n")
            return True
        except ApiError as exc:
            if exc.kind is ErrorKind.GEO_BLOCKED:
                print(f"  REFUSED [{exc.kind.value}]: {str(exc)[:100]}")
                print("  HTTP 451 is the server's REGION, not its address. "
                      "Waiting cannot fix it;\n  redeploy outside the US "
                      "(e.g. Frankfurt or Singapore).\n")
                return False
            if exc.kind is not ErrorKind.AUTH:
                print(f"  REFUSED [{exc.kind.value}]: {str(exc)[:100]}")
                print("  Not an allowlist problem, so waiting would only "
                      "delay the report.\n")
                return False
            reason = f"[{exc.kind.value}] {str(exc)[:80]}"
        except requests.RequestException as exc:
            reason = f"[network] {str(exc)[:80]}"

        remaining = deadline - time.time()
        if remaining <= 0:
            print(f"  still refused after {time.time() - started:.0f}s: "
                  f"{reason}")
            print("  -2015 covers the key, the allowlist and the key's "
                  "permissions together,\n  so check the Wallet permission "
                  "too before assuming it is the address.\n")
            return False
        print(f"  attempt {attempt}: {reason}  "
              f"(retrying, {remaining:.0f}s left)")
        time.sleep(min(cfg.auth_wait_poll_s, remaining))


def preflight(cfg: Config) -> int:
    """Probe every endpoint and report which ones actually work."""
    client = PredictionClient(cfg)
    print("\n=== Preflight ===\n")
    failures = 0

    ip = outbound_ip(client.session)
    print(f"==> Outbound IP: {ip or 'could not be determined'}")
    if cfg.auth_wait_timeout_s > 0:
        print("    This exact address must be on the API key's allowlist -- "
              "not the CIDR\n    range a host's dashboard shows, which "
              "Binance cannot parse.")
        print(f"    Waiting up to {cfg.auth_wait_timeout_s:.0f}s for a signed "
              f"request to be accepted,\n    retrying every "
              f"{cfg.auth_wait_poll_s:.0f}s. Add it now.\n")
        if not wait_for_auth(cfg, client):
            failures += 1
    else:
        print("    Set auth_wait_timeout_s (or AUTH_WAIT_S) to have preflight "
              "wait here\n    while you add it to the allowlist.")
    print()

    def check(label, fn):
        nonlocal failures
        try:
            print(f"  {label:<24} OK   {fn()}")
        except Exception as exc:            # noqa: BLE001 - report everything
            failures += 1
            print(f"  {label:<24} FAIL {type(exc).__name__}: {str(exc)[:105]}")

    check("public spot", lambda: f"{cfg.symbol} {client.spot_price():,.2f}")
    check("clock sync", lambda: f"offset {client.sync_clock()} ms")
    def vol_check() -> str:
        est = VolatilityEstimator(cfg, client.session)
        sigma = est.sigma_annual()
        raw = est.raw_sigma()
        note = ""
        if est.is_clamped():
            note = (f"  <-- CLAMPED from a measured {raw:.4f}; trading refused "
                    f"while this holds")
        df = est.tail_df()
        df_s = "gaussian" if df is None else f"{df:.2f}"
        return (f"sigma {sigma:.4f} (measured {raw:.4f}) "
                f"tail_df {df_s}{note}")

    check("volatility", vol_check)
    check("wallet", lambda: client.wallet().address)
    def balance_check() -> str:
        options = client.payment_options()
        parts = [f"{t}={b:.2f}{'' if en else ' (disabled)'}"
                 for t, b, en in options]
        in_wallet = client.prediction_wallet_value()
        if in_wallet is not None:
            parts.append(f"PREDICTION_WALLET={in_wallet:.2f}")
        breakdown = ", ".join(parts) or "none"
        bal = client.balance_usdt()

        notes = [f"{bal:.2f} USDT  [{breakdown}]"]
        if bal < cfg.min_stake_usdt:
            notes.append(f"  <-- below the {cfg.min_stake_usdt:.2f} minimum "
                         f"order size; no order can be placed")
        elif cfg.last_minute:
            # The last-minute path never calls kelly_stake either, so probing
            # it would answer a question about a strategy this profile does
            # not run. Report the sizing that IS in force, and the two limits
            # that can silently zero it out.
            per_round = bal * cfg.last_minute_stake_pct
            spendable = bal * (1.0 - cfg.reserve_pct)
            notes.append(f"  -> last-minute stakes {per_round:.2f} per round; "
                         f"{spendable:.2f} spendable after the "
                         f"{cfg.reserve_pct:.0%} reserve")
            if per_round < cfg.min_stake_usdt:
                notes.append(
                    f"  <-- UNTRADEABLE: {per_round:.2f} per round is under "
                    f"the {cfg.min_stake_usdt:.2f} venue minimum, so no "
                    f"round will ever be entered. Raise "
                    f"last_minute_stake_pct (now "
                    f"{cfg.last_minute_stake_pct:.0%}) or add funds.")
            elif spendable < per_round:
                notes.append(
                    f"  <-- UNTRADEABLE: one round needs {per_round:.2f} but "
                    f"only {spendable:.2f} is spendable. Lower reserve_pct "
                    f"(now {cfg.reserve_pct:.0%}) or last_minute_stake_pct.")
        elif cfg.straddle:
            # The straddle path never calls kelly_stake, so probing it here
            # would answer a question about a strategy this profile does not
            # run. Report the sizing that IS in force -- and the two limits
            # that can silently zero it out, which is precisely what went
            # unnoticed until the bot sat idle in live mode.
            per_side = bal * cfg.straddle_stake_pct
            spendable = bal * (1.0 - cfg.reserve_pct)
            notes.append(f"  -> straddle stakes {per_side:.2f} per leg, "
                         f"{per_side * 2:.2f} per round; {spendable:.2f} "
                         f"spendable after the {cfg.reserve_pct:.0%} reserve")
            if per_side < cfg.min_stake_usdt:
                notes.append(
                    f"  <-- UNTRADEABLE: {per_side:.2f} per leg is under the "
                    f"{cfg.min_stake_usdt:.2f} venue minimum, so no round "
                    f"will ever be entered. Raise straddle_stake_pct "
                    f"(now {cfg.straddle_stake_pct:.0%}) or add funds.")
            elif spendable < per_side * 2:
                notes.append(
                    f"  <-- UNTRADEABLE: one round needs {per_side * 2:.2f} "
                    f"but only {spendable:.2f} is spendable. Lower "
                    f"reserve_pct (now {cfg.reserve_pct:.0%}) or "
                    f"straddle_stake_pct.")
        else:
            # Ask the real sizing function, not a percentage rule of thumb:
            # on a small balance the Kelly fraction binds long before the cap.
            # Probe at the midpoint of THIS profile's entry band. A fixed
            # 0.60 lies outside the buffer and convex bands entirely, so the
            # verdict described a trade those profiles would never make.
            probe = (cfg.min_entry_price + cfg.max_entry_price) / 2.0
            be = breakeven_probability(probe, cfg.fee_bps)
            strong = kelly_stake(bal, min(be + 0.10, 0.999), probe, cfg)
            weak = kelly_stake(bal, min(be + 0.01, 0.999), probe, cfg)
            if strong <= 0:
                forced = cfg.min_stake_usdt / bal
                notes.append(
                    f"  <-- UNTRADEABLE: even a strong edge sizes below the "
                    f"{cfg.min_stake_usdt:.2f} minimum, and the override is "
                    f"blocked ({forced:.0%} of bankroll exceeds the "
                    f"{cfg.hard_max_stake_pct:.0%} hard cap or 2x Kelly).")
            else:
                mult = kelly_multiple(strong, bal, min(be + 0.10, 0.999),
                                      probe, cfg.fee_bps)
                tag = f" at {mult:.2f}x full Kelly" if mult else ""
                notes.append(f"  -> strong edge stakes {strong:.2f}{tag}; "
                             f"marginal edge stakes {weak:.2f}")
        return "".join(notes)

    check("balance", balance_check)
    check("funding plan", lambda: (  # noqa: PLC3002
        lambda p: f"accountType={p[0]} fundingSource={p[1]} holder={p[2]}"
    )(client.funding_plan()))
    check("daily quota", lambda: f"{client.remaining_quota_usdt()}")

    rounds: list[Round] = []

    def list_rounds():
        rounds.extend(client.list_rounds())
        found = sorted({r.symbol for r in rounds})
        where = ", ".join(found) if found else "none"
        return f"{len(rounds)} live round(s): {where}"

    check("market list", list_rounds)

    if rounds:
        rnd = rounds[0]
        hydrated: list[Round] = []

        def hydrate():
            h = client.hydrate(rnd)
            if h is None:
                raise ApiError("no startPrice in variantData")
            hydrated.append(h)
            return f"strike {h.strike:,.2f} feed {h.feed_symbol}"

        check("market detail", hydrate)
        check("venue parameters", lambda: (
            f"fee {rnd.fee_bps}bps  chain {rnd.chain_id}  "
            f"collateral {rnd.collateral}  precision {rnd.decimal_precision}  "
            f"slippage venue={rnd.venue_slippage_bps} used="
            f"{client.effective_slippage_bps(rnd)}  "
            f"liquidity {rnd.liquidity:,.0f}  "
            f"duration {rnd.duration_ms/1000:.0f}s"))
        check("order book",
              lambda: f"{len(client.asks_for(rnd, Side.UP) or [])} ask levels")
        check("settled history",
              lambda: f"{client.settled_outcome(rnd)}")
        check("redeem status",
              lambda: f"{client.redeem_status('0x0') or 'reachable'}")
        if hydrated:
            check("quote (no order)", lambda: (
                f"avg {client.get_quote(hydrated[0], Side.UP, cfg.min_stake_usdt).average_price:.4f}"))
    else:
        print("  (no live rounds -- detail/book/quote checks skipped)")

    print()
    if failures:
        print(f"{failures} check(s) failed.\n"
              "Override paths in the config file under \"endpoints\":\n"
              '  {"order_book": "/sapi/v1/w3w/wallet/prediction/order-book"}\n'
              "Valid keys: " + ", ".join(sorted(DEFAULT_ENDPOINTS)))
        return 1
    print("All probed endpoints OK. place-order-bundle is NOT probed here --\n"
          "it would spend money. Confirm it with one minimum-size manual\n"
          "trade before --live.")
    return 0


def whoami(cfg: Config, samples: int = 8) -> int:
    """
    Report the outbound IP this process actually uses.

    Render's Connect menu shows shared CIDR *ranges*, while Binance's API-key
    allowlist accepts individual addresses only. Pasting one address out of a
    /24 matches only when that address happens to be the one used, which is
    why the failure looks intermittent. Sampling repeatedly makes the churn
    visible instead of guessed at.
    """
    import collections

    session = requests.Session()
    seen: collections.Counter = collections.Counter()

    print(f"\nSampling the outbound IP {samples} times...\n")
    for i in range(samples):
        got = outbound_ip(session)
        if got:
            seen[got] += 1
            print(f"  sample {i+1}: {got}")
        else:
            print(f"  sample {i+1}: could not determine")
        time.sleep(0.4)

    if not seen:
        print("\nNo IP could be determined. Outbound HTTP may be blocked.")
        print("  Tried: " + ", ".join(IP_SERVICES))
        print("  Re-run with --verbose to see why each one failed.")
        return 1

    print(f"\n  distinct addresses observed: {len(seen)}")
    for ip, count in seen.most_common():
        print(f"    {ip}  ({count}/{samples})")

    print("\n  What to do with this:")
    if len(seen) > 1:
        print("    The address CHANGED between requests. An allowlist keyed to")
        print("    any single one of these will fail intermittently. You need")
        print("    dedicated/static egress, or no IP restriction on the key.")
    else:
        print("    Stable across this sample -- but a shared range can still")
        print("    reassign it on the next deploy or restart. Stability over")
        print("    eight requests is not a guarantee across deploys.")
    print("    Paste the exact address(es) above into the Binance key's")
    print("    allowlist -- not the CIDR range from Render's Connect menu,")
    print("    which Binance cannot parse.")

    # Prove whether Binance itself accepts us, which is the real question.
    print("\n  Checking whether Binance accepts this source...")
    client = PredictionClient(cfg)
    try:
        client.sync_clock()
        client.wallet()
        print("    OK: a signed request succeeded from this IP.")
        return 0
    except ApiError as exc:
        print(f"    FAIL [{exc.kind.value}]: {exc}")
        if exc.kind is ErrorKind.AUTH:
            print("    -2015 covers key, IP and permissions together. If the")
            print("    address above is allowlisted, check the key's Wallet")
            print("    permission next -- the code does not distinguish them.")
        return 1


def discover_min(cfg: Config) -> int:
    """Measure the venue's real minimum order size instead of assuming it."""
    client = PredictionClient(cfg)
    client.sync_clock()
    rounds = client.list_rounds()
    if not rounds:
        print("No live rounds to probe.")
        return 1

    rnd = client.hydrate(rounds[0]) or rounds[0]
    print(f"\nProbing {rnd.slug} (fee {rnd.fee_bps} bps, "
          f"liquidity {rnd.liquidity:,.0f}, precision {rnd.decimal_precision}, "
          f"chain {rnd.chain_id}, collateral {rnd.collateral})")
    print("Quotes are non-binding; no order is placed.\n")

    # Show the raw outcome at each size before drawing any conclusion.
    print("  probe results:")
    sizes = [1.0, 1.5, 2.0, 5.0, 10.0, 25.0]
    verdicts: list[tuple[float, bool, str]] = []
    for amount in sizes:
        try:
            q = client.get_quote(rnd, Side.UP, amount)
            verdicts.append((amount, True, (f"avg {q.average_price:.4f} "
                                           f"impact {q.price_impact:.4f}")))
        except ApiError as exc:
            verdicts.append((amount, False, str(exc)[:130]))
    for amount, ok, detail in verdicts:
        print(f"    {amount:>6.2f} USDT  {'OK  ' if ok else 'FAIL'}  {detail}")

    if not any(ok for _, ok, _ in verdicts):
        print("\n  Every size failed, so this is NOT a minimum-size problem.")
        print("  The message above is the venue's own; read it literally.")
        print("  Common causes: the quote request is missing or malformed a")
        print("  parameter, the wallet is not authorised for trading, or the")
        print("  token id is not tradable in this round.")
        print("\n  Re-run with --verbose to see the full signed request.")
        return 1

    smallest_ok = next((a for a, ok, _ in verdicts if ok), None)

    try:
        found = client.discover_min_stake(rnd, Side.UP)
    except ApiError as exc:
        # Never discard what the probe table already established.
        print(f"\n  Refinement stopped [{exc.kind.value}]: {exc}")
        if smallest_ok is not None:
            print(f"  Probing already showed {smallest_ok:.2f} USDT quotes "
                  f"successfully, so the minimum is at or below that.")
            return 0
        return 1

    if found is None:
        if smallest_ok is not None:
            print(f"\n  Search did not converge, but {smallest_ok:.2f} USDT "
                  f"quoted successfully -- treat that as the practical "
                  f"minimum.")
            return 0
        print("\n  No tested size quoted successfully.")
        return 1

    print(f"  Smallest quotable amount: ~{found:.2f} USDT")
    print(f"  Configured min_stake_usdt: {cfg.min_stake_usdt:.2f} USDT")
    if found > cfg.min_stake_usdt:
        print("\n  Your configured minimum is BELOW what this market accepts.")
        print(f"  Orders would be rejected. Consider min_stake_usdt={found:.2f}.")
    else:
        print("\n  Configured minimum is acceptable for this market.")
    print("  Note: this varies by market depth, so it is a snapshot, not a"
          " constant.")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _parse_symbols_arg(raw: str) -> tuple[str, ...]:
    """
    Comma-separated tickers -> a normalised tuple, e.g. "btc,eth" -> BTC,ETH.

    Shared by --symbols and the SYMBOLS environment variable so the two
    cannot silently drift apart. An empty or blank string yields (), which
    means "no restriction" -- the same as leaving the setting unset.
    """
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BTC 5m prediction market trader")
    # Tri-state on purpose. Left unset, the config file governs and the mode
    # can be changed by editing it while the bot runs. Set either way, the
    # flag pins the mode and file edits to `live` are ignored with a warning,
    # so a pinned mode can never appear to change when it cannot.
    ap.add_argument("--live", dest="live", action="store_const", const=True,
                    default=None, help="trade real money (pins the mode)")
    ap.add_argument("--paper", dest="live", action="store_const", const=False,
                    help="simulate only (pins the mode)")
    ap.add_argument("--preflight", action="store_true",
                    help="probe endpoints with your keys and exit")
    ap.add_argument("--calibration-report", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="is the edge real? compares realised win rate "
                         "against the breakeven for the price paid")
    ap.add_argument("--report-every", type=int, default=0,
                    help="log the calibration report every N settled trades; "
                         "useful when hosted, where the journal is hard to read")
    ap.add_argument("--report-profile", default=None,
                    help="restrict the report to one profile; default is "
                         "every profile in the journal, separately")
    # No default: an argparse default would always beat the file's
    # active_profile, making that setting dead exactly as --kelly's 0.25
    # default silently overrode every profile's kelly_fraction.
    ap.add_argument("--profile", choices=sorted(PROFILES), default=None,
                    help="buffer = big buffer late in the round; micro = "
                         "small accounts (default); favorite = favourites; "
                         "balanced = symmetric; convex = longshots")
    ap.add_argument("--scale-in", action="store_true", default=None,
                    help="open small, then top up while the round stays "
                         "favourable (never after a loss)")
    ap.add_argument("--no-scale-in", dest="scale_in", action="store_false")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated markets to RESTRICT trading to, "
                         "e.g. BTCUSDT,ETHUSDT. Default: none, meaning every "
                         "5m up/down market the venue lists is discovered "
                         "and traded automatically. Each trades "
                         "independently: its own position slot, loss streak "
                         "and calibration. Same as the SYMBOLS environment "
                         "variable; this flag wins if both are set.")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="how many markets may hold a position at once")
    ap.add_argument("--report-symbol", default=None,
                    help="restrict a report to one market")
    ap.add_argument("--min-buffer", type=float, default=None,
                    help="override the buffer gate, in sigmas of the time "
                         "remaining")
    ap.add_argument("--min-return", type=float, default=None,
                    help="minimum net profit per unit staked on a win, after "
                         "fees; 0.25 means a win must pay at least 25%% and "
                         "caps the fill price accordingly. 0 disables it.")
    ap.add_argument("--trend-follow", dest="trend_follow",
                    action="store_true", default=None,
                    help="enter earlier and stake more while the underlying "
                         "keeps running the same way")
    ap.add_argument("--no-trend-follow", dest="trend_follow",
                    action="store_false")
    ap.add_argument("--wait-for-auth", type=float, default=None,
                    help="seconds --preflight waits for a signed request to "
                         "be accepted, so a shared outbound IP can be added "
                         "to Binance's allowlist while it retries. Same as "
                         "the AUTH_WAIT_S environment variable; this flag "
                         "wins if both are set.")
    ap.add_argument("--whoami", action="store_true",
                    help="report the outbound IP actually in use and test "
                         "whether Binance accepts it")
    ap.add_argument("--discover-min", action="store_true",
                    help="probe the venue for its real minimum order size "
                         "(quotes only, places no order) and exit")
    ap.add_argument("--fee-bps", type=int, default=None,
                    help="override the fallback fee rate; each market's own "
                         "published rate is preferred when available")
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--no-fat-tails", action="store_true",
                    help="use a Gaussian model (understates cheap contracts)")
    ap.add_argument("--kelly", type=float, default=None,
                    help="override the profile's Kelly fraction")
    ap.add_argument("--paper-bankroll", type=float, default=None,
                    help="paper starting balance; defaults to the profile's "
                         "own figure so micro does not simulate a 100 USDT "
                         "account it will never have")
    ap.add_argument("--db", default="btc5m_journal.db")
    ap.add_argument("--config", default="config.json",
                    help="configuration file; created with --write-config. "
                         "Re-read automatically whenever it changes.")
    ap.add_argument("--write-config", action="store_true",
                    help="write a complete config file with every setting "
                         "at its current value, then exit")
    ap.add_argument("--print-default-profile", action="store_true",
                    help="print the default profile name and exit")
    ap.add_argument("--check-config", action="store_true",
                    help="validate the config file and exit; touches no "
                         "network and no journal")
    ap.add_argument("--no-hot-reload", action="store_true",
                    help="read the config once and ignore later edits")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s")

    # TRADING_MODE is the environment equivalent of --live/--paper, for
    # hosted deployments where the command line is fixed by the platform.
    # An explicit flag wins over it. Validated here, before any subcommand
    # returns, so a typo is caught even by a command that ignores the mode.
    live = args.live
    env_mode = os.environ.get("TRADING_MODE", "").strip().lower()
    if env_mode and env_mode not in ("live", "paper"):
        print(f"TRADING_MODE must be 'live' or 'paper', got {env_mode!r}",
              file=sys.stderr)
        return 1
    if live is None and env_mode:
        live = env_mode == "live"
        LOG.info("Mode pinned to %s by TRADING_MODE", env_mode.upper())

    # SYMBOLS is the environment equivalent of --symbols, same reasoning and
    # the same precedence: an explicit flag wins over it.
    env_symbols = os.environ.get("SYMBOLS", "").strip()

    # AUTH_WAIT_S is the environment equivalent of --wait-for-auth. Parsed
    # here, alongside TRADING_MODE and for the same reason: a typo must be
    # caught even by a subcommand that never reads the value, or it silently
    # becomes "do not wait" and takes the next deploy down with it.
    env_auth_wait: float | None = None
    raw_auth_wait = os.environ.get("AUTH_WAIT_S", "").strip()
    if raw_auth_wait:
        try:
            env_auth_wait = float(raw_auth_wait)
        except ValueError:
            print(f"AUTH_WAIT_S must be a number of seconds, got "
                  f"{raw_auth_wait!r}", file=sys.stderr)
            return 1

    if args.print_default_profile:
        # Exists so entrypoint.sh can read the default from the single source
        # of truth instead of repeating it in shell.
        print(DEFAULT_PROFILE)
        return 0

    if args.write_config:
        if os.path.exists(args.config):
            print(f"{args.config} already exists; refusing to overwrite.",
                  file=sys.stderr)
            return 1
        with open(args.config, "w", encoding="utf-8") as fh:
            json.dump(default_config_document(args.profile or DEFAULT_PROFILE),
                      fh,
                      indent=2, sort_keys=False)
            fh.write("\n")
        print(f"Wrote {args.config} with every setting at its current value.")
        print("Edit it while the bot runs; changes are picked up within a "
              "poll interval.")
        return 0

    if args.check_config:
        # Validates without touching the network or the journal, so an edit
        # can be checked before the running bot picks it up.
        if not os.path.exists(args.config):
            print(f"{args.config} does not exist.", file=sys.stderr)
            return 1
        try:
            with open(args.config, encoding="utf-8") as fh:
                document = json.load(fh)
            checked = build_config(document, api_key="x", api_secret="x",
                                   live=None, db_path=args.db,
                                   profile=args.profile)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            return 1
        print(f"OK: {args.config} is valid.")
        print(f"  profile        {checked.profile_name}")
        print(f"  entry band     {checked.min_entry_price:.2f}"
              f"-{checked.max_entry_price:.2f}")
        if checked.straddle:
            print(f"  max stake      "
                  f"{checked.straddle_stake_pct:.0%} of bankroll per leg, "
                  f"{checked.straddle_stake_pct * 2:.0%} per round")
        elif checked.last_minute:
            print(f"  max stake      "
                  f"{checked.last_minute_stake_pct:.0%} of bankroll "
                  f"per round")
            ceiling = ("no ceiling"
                       if checked.last_minute_max_price >= 1.0
                       else f"never above "
                            f"{checked.last_minute_max_price:.2f}")
            print(f"  entry rule     the dearer side at "
                  f"{checked.last_minute_price_floor:.2f}+ from "
                  f"{checked.last_minute_start_s:.0f}s, at any price from "
                  f"{checked.last_minute_fallback_s:.0f}s, nothing under "
                  f"{checked.last_minute_deadline_s:.0f}s; {ceiling}")
        else:
            print(f"  max stake      {checked.max_stake_pct:.0%} of bankroll")
        print(f"  min buffer     {checked.min_buffer_sigmas} sigma")
        print(f"  daily limit    {checked.daily_loss_limit_pct:.0%}")
        return 0

    if args.calibration_report:
        print(Journal(args.db).calibration_report(args.report_profile))
        return 0

    if args.diagnose:
        print(Journal(args.db).diagnose(args.report_profile,
                                        args.report_symbol))
        return 0

    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        print("Set BINANCE_API_KEY and BINANCE_API_SECRET.", file=sys.stderr)
        return 1

    overrides: dict = {}
    if args.kelly is not None:
        overrides["kelly_fraction"] = args.kelly
    if args.min_edge is not None:
        overrides["min_edge"] = args.min_edge
    if args.fee_bps is not None:
        overrides["fee_bps"] = args.fee_bps
    if args.paper_bankroll is not None:
        overrides["paper_start_bankroll"] = args.paper_bankroll
    if args.min_buffer is not None:
        overrides["min_buffer_sigmas"] = args.min_buffer
    if args.min_return is not None:
        overrides["min_win_return"] = args.min_return
    if args.trend_follow is not None:
        overrides["trend_follow"] = args.trend_follow
    # AUTH_WAIT_S is the environment equivalent of --wait-for-auth, for hosted
    # deployments where the command line is fixed by the platform. The flag
    # wins when both are set, matching --symbols and --live.
    if args.wait_for_auth is not None:
        overrides["auth_wait_timeout_s"] = args.wait_for_auth
    elif env_auth_wait is not None:
        overrides["auth_wait_timeout_s"] = env_auth_wait
    if args.scale_in is not None:
        overrides["scale_in"] = args.scale_in
    if args.report_every:
        overrides["report_every"] = args.report_every
    if args.no_fat_tails:
        overrides["use_fat_tails"] = False
    if args.symbols:
        overrides["symbols"] = _parse_symbols_arg(args.symbols)
    elif env_symbols:
        overrides["symbols"] = _parse_symbols_arg(env_symbols)
        LOG.info("Markets pinned to %s by SYMBOLS",
                 ", ".join(overrides["symbols"]) or "(empty -> every market)")
    if args.max_concurrent is not None:
        overrides["max_concurrent_positions"] = args.max_concurrent

    try:
        config_path = args.config if os.path.exists(args.config) else None
        if config_path is None and args.config != "config.json":
            print(f"Config file {args.config} not found.", file=sys.stderr)
            return 1
        store = ConfigStore(
            None if args.no_hot_reload and config_path is None else config_path,
            api_key=key, api_secret=secret, live=live, db_path=args.db,
            profile=args.profile, overrides=overrides)
        cfg = store.current
        if config_path:
            LOG.info("Config from %s%s", config_path,
                     "" if not args.no_hot_reload else " (hot reload off)")
        else:
            LOG.info("No config file; using built-in defaults. "
                     "Run --write-config to create one.")
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    # Every command is wrapped: an uncaught exception reaching the user as a
    # traceback is a defect in its own right, regardless of the cause.
    commands = {
        "preflight": (args.preflight, preflight),
        "discover-min": (args.discover_min, discover_min),
        "whoami": (args.whoami, whoami),
    }
    for name, (selected, fn) in commands.items():
        if not selected:
            continue
        try:
            return fn(cfg)
        except ApiError as exc:
            print(f"\n{name} failed [{exc.kind.value}]: {exc}", file=sys.stderr)
            if exc.kind is ErrorKind.AUTH:
                print("  Check the API key's Wallet permissions and IP "
                      "allowlist.", file=sys.stderr)
            elif exc.kind is ErrorKind.INSUFFICIENT_FUNDS:
                print("  The account balance cannot cover the requested size.",
                      file=sys.stderr)
            elif exc.kind is ErrorKind.TIMING:
                print("  System clock drift; re-sync and retry.",
                      file=sys.stderr)
            elif exc.kind is ErrorKind.GEO_BLOCKED:
                print("  HTTP 451: Binance blocks this server's region. If you "
                      "are hosting,\n  redeploy to a non-US region "
                      "(e.g. Frankfurt or Singapore).", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130

    if cfg.live:
        print("\n*** LIVE MODE: this will spend real USDT. ***")
        print("Confirm you have (1) run --preflight clean, and (2) reviewed")
        print("--calibration-report over several hundred paper rounds.")
        # if input('Type "I ACCEPT THE RISK" to continue: ') != "I ACCEPT THE RISK":
        #     print("Aborted.")
        #     return 1

    try:
        Trader(store if not args.no_hot_reload else cfg).run()
    except ApiError as exc:
        print(f"\nStopped [{exc.kind.value}]: {exc}", file=sys.stderr)
        return 1
    except Exception:                        # noqa: BLE001
        # run() has already logged the traceback and drained. What is left is
        # to exit non-zero with a pointer to the journal, rather than dumping
        # a second copy of the same traceback onto the operator.
        print("\nStopped by an unexpected error; see the log above.",
              file=sys.stderr)
        print("  Open positions were drained before exit. Check the journal "
              "with --calibration-report before restarting.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
