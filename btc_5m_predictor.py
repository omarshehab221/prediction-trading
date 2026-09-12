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
    pip install requests websocket-client

FEEDS
-----
Order book, spot price and the volatility window arrive on persistent
sockets; see ws_feeds.py. Everything signed and mutating stays on REST.
Set `ws_enabled` to false in the config to go back to REST for everything,
which is what this did before the sockets existed.
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

import ws_feeds
from btc5m.constants import (
    BASIS_EWMA_ALPHA,
    BASIS_EWMA_MIN_SAMPLES,
    DEFAULT_FEE_BPS,
    DEFAULT_ROUND_SECONDS,
    EPS,
    LOG,
    WS_BOOK_VALIDATE_TOL,
)
from btc5m.units import WEI, _as_float_or_none, from_wei, to_wei
from btc5m.venue.endpoints import BASE, CEX_ACCOUNT_TYPES, DEFAULT_ENDPOINTS
from btc5m.errors import (
    ApiError,
    ERROR_CODES,
    ErrorKind,
    NothingToRedeem,
    OrderNotFilled,
    Shutdown,
    TradingHalted,
    _ALREADY_REDEEMED_HINTS,
    _AboveBalance,
    _MESSAGE_HINTS,
    _is_already_redeemed,
)
from btc5m.stats import (
    _betacf,
    betainc,
    norm_cdf,
    standardised_t_cdf,
    student_t_cdf,
)
from btc5m.pricing import (
    bracket_prices,
    breakeven_probability,
    buffer_sigmas,
    buy_reservation_price,
    digital_up_probability,
    max_price_for_return,
    price_for_breakeven,
    sell_reservation_price,
    signal_edge_required,
    win_return,
)
from btc5m.pnl import settle_pnl, straddle_worst_case_pnl, wins_per_loss
from btc5m.sizing import (
    boosted_stake,
    kelly_multiple,
    kelly_stake,
    max_topup_within_blend,
    straddle_completion_stake,
    straddle_split,
    walk_book,
)
from btc5m.domain import (
    Action,
    Bracket,
    OrderPlan,
    OrderState,
    OrderType,
    PendingOrder,
    Position,
    Quote,
    Round,
    Side,
    Signal,
    Trend,
    WalletRef,
    _market_buy,
)
from btc5m.config import Config
from btc5m.profiles import DEFAULT_PROFILE, PROFILES
from btc5m.config_file import (
    CONFIG_SCHEMA_NOTE,
    ConfigStore,
    DEFERRED_FIELDS,
    IMMUTABLE_FIELDS,
    _coerce,
    build_config,
    default_config_document,
)

# Run as a script, this file is the main module -- and ws_feeds' lazy
# `from btc_5m_predictor import Side` would then import it a SECOND time,
# producing a second Side class. `side is Side.UP` fails across those two
# classes, so the UP ladder came back derived from the DOWN side of the book.
# Registering the running module under its own name makes that import find
# this module instead of loading another copy of it.
if __name__ != "btc_5m_predictor":
    sys.modules.setdefault("btc_5m_predictor", sys.modules[__name__])

# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Configuration file and hot reload
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Domain types
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


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


class VolatilityEstimator:
    """Annualised sigma AND tail thickness from recent 1m returns."""

    def __init__(self, cfg: Config | ConfigStore, market_data) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._market_data = market_data
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

        # Through the seam rather than fetched here: the socket keeps a
        # window that is already current, and the REST fetch behind it is
        # the same one this used to make.
        closes = self._market_data.closes(symbol)
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

        NOTHING IS EVER LEFT OVER
        -------------------------
        However the residue is split, ALL of it is absorbed, so that after
        this returns

            _day_start_bankroll + _realised_pnl == bankroll

        exactly. The venue's number is the truth and this pair is only a
        decomposition of it; a decomposition that does not add up is just a
        slower way of being wrong.

        This used to return early whenever the drift was under `tolerance`,
        on the reasoning that something smaller than a fraction of the venue
        minimum cannot be a trade. True, and irrelevant: the drift is
        measured against the baseline ABSOLUTELY, not since the last call, so
        an unabsorbed cent is measured again next time and every time after.
        A model that runs a few cents light per round therefore sat under the
        threshold, invisible, until the accumulated total crossed it in one
        step -- at which point the whole accumulation was rebased into the
        baseline as somebody's deposit. The threshold turned a steady leak
        into a periodic laundering, which is precisely the drift it was meant
        to be too small to cause.

        So the threshold now governs REPORTING only: dust is absorbed
        quietly, a real movement is absorbed and announced. Either way the
        identity above holds on the way out.
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
        if abs(drift) <= EPS:
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
        if abs(drift) <= EPS:
            return 0.0

        self._day_start_bankroll = max(self._day_start_bankroll + drift, EPS)
        self._external_flow += drift
        if abs(drift) > tolerance:
            LOG.info("External balance movement %+.2f USDT (deposit, "
                     "withdrawal or an order this bot did not place); "
                     "baseline rebased to %.2f. Not counted as a trading "
                     "result.", drift, self._day_start_bankroll)
        else:
            # Too small to be a trade or a transfer, and still absorbed --
            # see NOTHING IS EVER LEFT OVER above.
            LOG.debug("Absorbed %+.4f USDT of unattributable drift; baseline "
                      "now %.2f", drift, self._day_start_bankroll)
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

        Every array and object parameter is a JSON string, which is what the
        connector does and therefore what the venue expects. See below.
        """
        p = {k: v for k, v in params.items() if v is not None}
        # Arrays and objects go as JSON, because that is what the venue is
        # given by its own connector. @binance/common routes every parameter
        # through serializeValue, which JSON.stringify's anything that is not
        # a scalar, and then HMACs the string it built -- so the JSON form is
        # both what arrives and what the signature is checked against.
        #
        # This used to send flat lists as repeated parameters
        # (tokenIds=a&tokenIds=b) via urlencode's doseq. That was a guess
        # carried from the first commit which no live call ever tested:
        # batch_redeem cannot run until a real position exists, and
        # place-order-bundle has never executed against a funded account, so
        # the only two array parameters in the bot -- tokenIds and
        # cancelInfoList -- had never left the machine. Repeated parameters
        # are not a Binance array convention anywhere.
        #
        # doseq is therefore gone rather than left set: no list or tuple can
        # reach urlencode any more, so a doseq=True here would describe a
        # path that no longer exists.
        p = {k: (json.dumps(v, separators=(",", ":"))
                 if isinstance(v, (dict, list, tuple)) else v)
             for k, v in p.items()}
        p["timestamp"] = self.now_ms()
        p["recvWindow"] = self._cfg.recv_window_ms
        query = urllib.parse.urlencode(sorted(p.items()))
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

    def kline_closes(self, symbol: str, limit: int) -> list[float]:
        """
        Recent 1m closes, most recent last.

        Lives here rather than in the volatility estimator because two
        callers need it now: the estimator, and the socket feed seeding its
        window. One fetch, one parse, one place to fix.
        """
        r = self._session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": limit},
            timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]

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

    @staticmethod
    def _parse_levels(payload: dict,
                      key: str) -> list[tuple[float, float]] | None:
        """
        One side of the book. Levels are {price, size} strings per the schema.

        Takes the key rather than assuming "asks", because selling prices off
        bids and the two sides are parsed by identical rules -- a second copy
        of this would be a second place for the skipped-level accounting to
        drift.
        """
        raw = payload.get(key)
        if raw is None:
            nested = payload.get("orderBook") or payload.get("data") or {}
            raw = nested.get(key) if isinstance(nested, dict) else None
        if not raw:
            return None
        if not isinstance(raw, (list, tuple)):
            # A scalar here is malformed; iterating it raises TypeError.
            LOG.warning("Order book %r is %s, not a list",
                        key, type(raw).__name__)
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
            LOG.warning("Order book %r: skipped %d unparseable level(s) "
                        "of %d", key, skipped, len(raw))
        ordered = sorted(levels)
        # Asks read cheapest-first and bids read dearest-first, so [0] is the
        # touch on either side and no caller has to remember which is which.
        if key == "bids":
            ordered.reverse()
        return ordered or None

    @staticmethod
    def _parse_asks(payload: dict) -> list[tuple[float, float]] | None:
        """Ask ladder. Kept by name: TestParseAsks and fuzz.py both call it."""
        return PredictionClient._parse_levels(payload, "asks")

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
            price_limit=plan.price_limit)

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
                self.get_quote(rnd, _market_buy(side, amount))
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
        Phase 2: execute a quote.

        MARKET orders are FOK -- fill-or-kill, so there are no partial fills
        at prices the model never approved. LIMIT orders are GTC and REST:
        the id returned describes an order that may sit on the book for
        minutes, fill in pieces, or never fill at all. Confirming a limit
        order with confirm_fill would raise on the ordinary case; use
        order_state instead.

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
        failed: dict[str, str] = {}
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
                return self._read_order(order)
        found = self.order_fill(order_id)
        if found is None:
            return None
        return self._read_order(found)

    def _read_order(self, order: dict) -> OrderState:
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
        return OrderState(state, filled_usdt, filled_shares, price)

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
                settle_source TEXT, order_type TEXT, price_limit REAL,
                exit_price REAL, exit_order_id TEXT)""")
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
        for col, decl in (("order_type", "TEXT"), ("price_limit", "REAL"),
                          ("exit_price", "REAL"), ("exit_order_id", "TEXT")):
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        self._conn.commit()

    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None,
               order_type: str = "MARKET",
               price_limit: float | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (ts, mode, slug, topic_id, side, strike, spot,"
            " sigma, seconds_left, end_ms, model_prob, fill_price, edge, stake,"
            " bankroll_before, order_id, profile, buffer_z, fee_bps, symbol,"
            " trend_z, order_type, price_limit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mode, rnd.slug, rnd.topic_id, sig.side.value,
             rnd.strike, spot, sigma, sig.seconds_left, rnd.end_ms,
             sig.model_prob, sig.fill_price, sig.edge, sig.stake_usdt,
             bankroll, order_id, self._profile, sig.buffer_z, rnd.fee_bps,
             rnd.symbol, sig.trend_z, order_type, price_limit))
        self._conn.commit()
        return int(cur.lastrowid)

    def resolve(self, trade_id: int, won: bool, pnl: float,
                source: str) -> None:
        self._conn.execute(
            "UPDATE trades SET resolved=1, won=?, pnl=?, settle_source=?"
            " WHERE id=?", (1 if won else 0, pnl, source, trade_id))
        self._conn.commit()

    def close(self) -> None:
        """
        Release the SQLite handle.

        The connection was never closed, which on Windows means the file
        cannot be unlinked while the process lives -- every test that removes
        its temp journal fails on the unlink, and the real failure is hidden
        behind a cleanup error. A long-running process leaks one handle per
        Journal, which is small but no more correct.
        """
        self._conn.close()

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
        # Sold rows are resolved but have no outcome to be calibrated
        # against: the position was closed before the round decided
        # anything. Pooling them with settled rows would let an exit taken at
        # a good price look like a correct prediction, which is exactly the
        # inference this report exists to make impossible.
        where = "WHERE resolved=1 AND COALESCE(settle_source, '') != 'sold'"
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
        sold = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades"
            " WHERE resolved=1 AND settle_source='sold'").fetchone()
        if not rows:
            return "No resolved trades yet."

        buckets: dict[int, list] = {}
        for price, won, pnl, stake, fee in rows:
            buckets.setdefault(int(price * 20), []).append(
                (price, won, pnl or 0.0, stake or 0.0, fee))

        scope = symbol or "all markets"
        out = [f"Trades analysed : {len(rows)}  ({scope})"]
        if sold and sold[0]:
            out.append(f"Excluded        : {sold[0]} sold before settlement "
                       f"({sold[1]:+.2f} USDT)")
        out += ["",
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


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


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

    order_state and cancel_orders are named exactly as PredictionClient's, so
    the reaper has one body and paper cannot drift into a shortcut through
    the lifecycle it exists to exercise.
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
            crossing = [(pr, sz) for pr, sz in levels if pr <= limit + EPS]
            shares, spent = 0.0, 0.0
            for price, size in crossing:
                take = min(size, (plan.amount - spent) / price)
                if take <= 0:
                    break
                shares += take
                spent += take * price
            return shares, spent, (spent / shares if shares > 0 else None)
        levels = self._market_data.bids(rnd, plan.side) or []
        crossing = [(pr, sz) for pr, sz in levels if pr >= limit - EPS]
        shares, proceeds = 0.0, 0.0
        for price, size in crossing:
            take = min(size, plan.amount - shares)
            if take <= 0:
                break
            shares += take
            proceeds += take * price
        return shares, proceeds, (proceeds / shares if shares > 0 else None)


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
        self._market_data = ws_feeds.MarketData(
            self._client, self._store or self._static_cfg)
        self._vol = VolatilityEstimator(cfg, self._market_data)
        # One seam for orders, so paper and live share the whole lifecycle
        # rather than paper taking a shortcut through it.
        self._paper_book = PaperBook(self._market_data)
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
        # order_id -> the resting order behind it. Separate from _positions
        # because an order is not a position until something fills: counting
        # one as the other books a trade that may never happen, and not
        # tracking it at all abandons one that did.
        self._pending: dict[str, PendingOrder] = {}
        # Scalp state, keyed to match _positions and _seen so one prune pass
        # can clear all of it together.
        #
        # _scalp_entries counts round TRIPS per round rather than marking the
        # round seen: every other strategy enters a round once and writes
        # _seen, and doing that here would cap this profile at one trade in
        # the five minutes it exists to trade repeatedly.
        self._brackets: dict[tuple[str, Side], Bracket] = {}
        # topic_id -> (end_ms, entries taken on this round).
        self._scalp_entries: dict[int, tuple[int, int]] = {}
        self._flattened: set[int] = set()
        # symbol -> monotonic seconds of the last entry, for the cooldown.
        self._scalp_last_entry: dict[str, float] = {}
        # symbol -> (EWMA of the perp/spot basis in bps, samples seen).
        self._basis_ewma: dict[str, tuple[float, int]] = {}
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
    def _orders(self):
        """The venue in live mode, the simulator in paper mode."""
        return self._client if self._live else self._paper_book

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
        horizon = now_ms - self._cfg.prune_after_s * 1000
        for tid in [t for t, end in self._seen.items() if end < horizon]:
            self._seen.pop(tid, None)
            self._hydrated.pop(tid, None)
        # The scalp counters are keyed by topic and are deliberately NOT
        # written to _seen -- that would cap this profile at one trade in the
        # five minutes it exists to trade repeatedly -- so they carry their
        # own end_ms and get their own sweep. Without it they grow by one
        # entry per round for the life of the process.
        for tid in [t for t, (end, _) in self._scalp_entries.items()
                    if end < horizon]:
            self._scalp_entries.pop(tid, None)
            self._flattened.discard(tid)

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
            self._market_data.start()
            feeds = self._market_data.status()
            LOG.info("Feeds: spot %s, book %s, futures %s", feeds["spot"],
                     feeds["book"], feeds["futures"])
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
                    # Before settlement, always. A fill has to become a
                    # position before its round is allowed to settle, or the
                    # position settles as though it had never been opened.
                    self._reap_pending()
                    # After the reaper, so a take-profit that filled this
                    # pass has already closed its position and disarmed its
                    # own stop; before the flatten, so a stop that is due
                    # fires at its own price rather than at whatever the
                    # deadline can reach.
                    self._check_stops()
                    self._flatten_scalps()
                    self._maybe_exit_all()
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
                    # Subscriptions follow the markets actually in
                    # play, and are set AFTER discovery rather than
                    # before it. Config.symbols defaults to empty --
                    # meaning trade everything the venue lists -- so
                    # the set is not knowable until the venue has
                    # been asked. Driving this off open positions
                    # instead would subscribe to nothing whenever the
                    # bot is flat, which is most of the time, and the
                    # feed would never carry a price to be healthy
                    # about.
                    self._market_data.track(
                        self._cfg.symbols
                        or {r.symbol for r in self._hydrated.values()})
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
        elif self._cfg.scalp:
            self._maybe_enter_scalp(bankroll, mode)
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
                quote = self._client.get_quote(raw, _market_buy(side, stake))
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

    def _cancel_all_pending(self) -> None:
        """
        Retract every resting order, then book whatever filled first.

        Delegates the cancelling to the reaper rather than doing its own
        pass. Cancelling here and then reaping sent every order to
        batch-cancel twice, and a second cancel of an order that filled in
        between reads as a fresh failure for a reason that no longer exists.
        """
        if not self._pending:
            return
        LOG.info("Cancelling %d resting order(s)", len(self._pending))
        self._reap_pending(force_final=True)

    def _reap_pending(self, force_final: bool = False) -> None:
        """
        Advance every resting order: book fills, retract what has run out.

        Called at the top of the loop, before settlement, because a fill has
        to become a position before its round settles.

        The cancel is a request, not an answer. batch-cancel reports an order
        under `failed` most often because it FILLED first, so nothing here
        reads that list: after any cancel the order's state is read again and
        whatever came back is booked. Treating a failed cancel as "still
        resting" walks away from a real position, which then settles, wins,
        and is never claimed because no journal row knows it exists.
        """
        if not self._pending:
            return
        now_ms = self._client.now_ms()
        for pending in list(self._pending.values()):
            # The order id travels ON the order rather than only as the dict
            # key, so a helper that is handed a PendingOrder can name the
            # order it is talking about without the caller passing it too.
            order_id = pending.order_id
            try:
                state = self._orders.order_state(order_id)
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

            self._retract(order_id,
                          "round over" if now_ms >= pending.rnd.end_ms
                          else "entry window closed")

    def _retract(self, order_id: str, reason: str) -> bool:
        """
        Cancel one resting order and book whatever the cancel raced.

        True when the order is done with and out of _pending; False when it
        could not be reached, in which case it is left pending and retried.

        The cancel is a request, not an answer. batch-cancel reports an order
        under `failed` most often because it FILLED first, so nothing here
        reads that list: the order's state AFTER the cancel is the answer,
        and whatever came back is booked. Reading `failed` as "still
        resting" walks away from a real position, which then settles, wins,
        and is never claimed because no journal row knows it exists.

        One body rather than two, because the reaper and the stop trigger
        need exactly the same discipline and the stop is the case where the
        race is most likely -- it fires precisely when the price is moving.
        """
        pending = self._pending.get(order_id)
        if pending is None:
            return True
        LOG.info("%s: retracting the %s %s order (%s)", pending.rnd.slug,
                 pending.plan.side.value, pending.plan.action.value, reason)
        try:
            self._orders.cancel_orders([order_id])
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Cancel failed for %s: %s", order_id, exc)
            return False
        try:
            final = self._orders.order_state(order_id)
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Could not re-read %s after cancel: %s", order_id, exc)
            return False
        if final is not None:
            self._book_fill(order_id, self._pending.get(order_id, pending),
                            final)
        self._pending.pop(order_id, None)
        return True

    def _book_fill(self, order_id: str, pending: PendingOrder,
                   state: OrderState) -> PendingOrder:
        """
        Record whatever this order has filled that is not recorded yet.

        min_fill_fraction is deliberately not consulted. It is the FOK guard,
        where a short fill means something went wrong; on a GTC order a short
        fill is the ordinary outcome, and refusing it strands shares the
        account already holds. Any non-zero fill becomes a position.
        """
        if pending.plan.action is Action.SELL:
            return self._book_sale(order_id, pending, state)
        new_usdt = state.filled_usdt - pending.filled_usdt
        if new_usdt <= EPS:
            return pending
        price = (state.price or pending.fill_price
                 or pending.plan.price_limit or 0.0)
        key = (pending.rnd.symbol, pending.plan.side)
        existing = self._positions.get(key)
        trade_id = pending.trade_id
        if existing is None:
            sig = replace(pending.signal, stake_usdt=state.filled_usdt,
                          fill_price=price)
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
        price = rnd.round_price(price)
        if not 0.0 < price < 1.0:
            return False
        plan = OrderPlan(side=sig.side, action=Action.BUY,
                         order_type=OrderType.LIMIT, amount=sig.stake_usdt,
                         price_limit=price)
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
        self._pending[str(order_id)] = PendingOrder(
            order_id=str(order_id), rnd=rnd, plan=plan,
            signal=replace(sig, fill_price=price),
            expires_at_ms=expires_at_ms,
            filled_usdt=0.0, filled_shares=0.0, trade_id=None)
        LOG.info("POST %s %s | limit %.4f model %.3f stake %.2f (%.0fs left)",
                 rnd.slug, sig.side.value, price, sig.model_prob,
                 sig.stake_usdt, sig.seconds_left)
        return True

    def _maybe_exit_all(self) -> None:
        """
        Consider leaving each open position before the round decides it.

        MARKET and LIMIT only. BRACKET is not a flavour of these: its exit
        prices come from the price the position filled at, not from a model
        reading of the book, and its legs are placed by _arm_bracket at entry
        rather than reconsidered every pass. Running this over a bracketed
        position would stack a second, model-priced offer on shares that
        already have one.
        """
        if self._cfg.exit_order_type not in ("MARKET", "LIMIT"):
            return
        offered = {(p.rnd.topic_id, p.plan.side)
                   for p in self._pending.values()
                   if p.plan.action is Action.SELL}
        for pos in list(self._positions.values()):
            if (pos.rnd.topic_id, pos.signal.side) in offered:
                continue                  # already offered; do not stack
            self._post_exit(pos)

    def _model_prob(self, pos: Position) -> float | None:
        """
        The model's current probability for the side this position holds.

        Follows _maybe_scale_in exactly, including the clamped-sigma refusal:
        an overstated sigma inflates the tail probabilities, and pricing an
        EXIT off an inflated probability holds out for a price the model only
        believes because its volatility estimate is broken.
        """
        if pos.rnd.strike is None:
            return None
        symbol = self._client.market_symbol(pos.rnd.feed_symbol)
        secs = pos.rnd.seconds_remaining(self._client.now_ms())
        if secs <= 0:
            return None
        try:
            spot = self._market_data.spot(symbol)
            sigma = self._vol.sigma_annual(symbol)
        except (ApiError, requests.RequestException) as exc:
            LOG.debug("%s: cannot price an exit yet: %s", pos.rnd.slug, exc)
            return None
        if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
            return None
        tail_df = self._vol.tail_df(symbol)
        p_up = digital_up_probability(spot, pos.rnd.strike, sigma, secs,
                                      tail_df)
        return p_up if pos.signal.side is Side.UP else 1.0 - p_up

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
            # The bid is already there, so cross it rather than queue behind.
            target = bids[0][0]
        target = pos.rnd.round_price(target)
        if not 0.0 < target < 1.0:
            return False

        shares = pos.committed_usdt / max(pos.signal.fill_price, EPS)
        order_type = (OrderType.LIMIT if self._cfg.exit_order_type == "LIMIT"
                      else OrderType.MARKET)
        plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                         order_type=order_type, amount=shares,
                         price_limit=(target if order_type is OrderType.LIMIT
                                      else None))
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
            signal=pos.signal, expires_at_ms=pos.rnd.end_ms,
            filled_usdt=0.0, filled_shares=0.0, trade_id=pos.trade_id)
        LOG.info("OFFER %s %s | %.4f shares at %.4f (entry %.4f)",
                 pos.rnd.slug, pos.signal.side.value, shares, target,
                 pos.signal.fill_price)
        return True

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
            pnl = state.filled_usdt - pos.committed_usdt
            self._journal.resolve_sold(pos.trade_id, state.filled_usdt,
                                       price, order_id, pos.committed_usdt)
            self._positions.pop(key, None)
            self._brackets.pop(key, None)
            self._record_sale(key[0], pnl)
            LOG.info("SOLD %s %s | %.4f USDT at %.4f (entry %.4f) P&L %+.4f",
                     pending.rnd.slug, pending.plan.side.value,
                     state.filled_usdt, price, pos.signal.fill_price, pnl)
        else:
            self._positions[key] = replace(pos, committed_usdt=remaining)
            LOG.info("%s: sold %.4f of %.4f USDT at %.4f; %.4f left to settle",
                     pending.rnd.slug, sold_cost, pos.committed_usdt, price,
                     remaining)
        return replace(pending, filled_usdt=state.filled_usdt,
                       filled_shares=state.filled_shares)

    def _record_sale(self, symbol: str, pnl: float) -> None:
        """
        Report a position closed by SELLING to the risk manager.

        THE GAP THIS CLOSES
        -------------------
        Every other ending reports itself from _settle_one. A sold position
        never reaches _settle_one -- it leaves through _book_sale -- so
        before this existed a profile that closes by selling reported no
        results at all: daily_loss_limit_pct saw nothing, the consecutive
        loss counter never moved, and the breaker meant to stop a bad day was
        wired to a path such a profile never takes.

        That was survivable while selling was rare. It is not survivable for
        `scalp`, which closes every position this way, twenty times a round.

        model_prob is deliberately not passed. The calibration statistics
        answer one question -- did the price paid predict the outcome -- and
        a position closed BEFORE the outcome existed has no answer to
        contribute. Feeding one in would put a number in that bucket that no
        round ever produced, which is the single figure this bot exists to
        get right.

        The paper bankroll moves here for the same reason: in paper mode
        _settle_one is what credits P&L, so without this a paper scalper
        would trade all day against a balance that never changed.
        """
        if not self._live:
            self._paper_bankroll += pnl
        self._risk_for(symbol).record_result(pnl > 0, pnl=pnl)
        if self._account_risk is not None:
            self._account_risk.record_result(pnl > 0, pnl=pnl)

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
            quote = self._client.get_quote(raw, _market_buy(side, stake))
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
                quote = self._client.get_quote(raw, _market_buy(other, stake))
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
                    quotes[side] = self._client.get_quote(
                        raw, _market_buy(side, stakes[side]))
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
            spot = self._market_data.spot(symbol)
            sigma = self._vol.sigma_annual(symbol)
            if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
                LOG.warning("Skipping %s: volatility clamped, so every edge "
                            "estimate would be unreliable", rnd.slug)
                continue
            tail_df = self._vol.tail_df(symbol)
            trend = self._vol.trend(symbol)
            book = {}
            for side in Side:
                levels = self._market_data.asks(rnd, side)
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

            if self._cfg.entry_order_type == "LIMIT":
                # Expiry is fixed here, from the window that authorised THIS
                # order, so a later config change or a different strategy's
                # window cannot retroactively extend or shorten it.
                window_end_ms = rnd.end_ms - int(
                    self._cfg.entry_window_end_s * 1000)
                if self._post_limit_entry(rnd, sig, spot, sigma, bankroll,
                                          mode, window_end_ms):
                    self._seen[rnd.topic_id] = rnd.end_ms
                    available -= sig.stake_usdt
                    if available < self._cfg.min_stake_usdt:
                        return
                continue

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
                quote = self._client.get_quote(rnd, _market_buy(sig.side, sig.stake_usdt))

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
                         quote.average_price, quote.amount_out)

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
                quote = self._client.get_quote(raw, _market_buy(side, stake))
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

    # -- scalping the futures lead ------------------------------------------

    def _basis_dislocation_bps(self, symbol: str) -> float | None:
        """
        How far the perp/spot basis sits from its own recent mean, in bps.

        The LEVEL of the basis says nothing about whether spot is lagging: it
        is dominated by funding, which is a persistent bias and not
        information. Gating on it would fire constantly on one side and never
        on the other -- a funding-rate detector wearing a lead-lag costume.
        The DEVIATION from its own recent mean is the quantity the premise
        actually describes: the perp has moved and spot has not followed yet.

        The mean is an EWMA advanced once per pass, which is the cheapest
        estimator that does not need a second ring buffer. The deviation is
        measured against the mean BEFORE this sample is folded in, or every
        observation would be partly averaged into its own baseline and the
        dislocation would report smaller than it is.

        None until the EWMA has seen enough samples to be a mean rather than
        the first observation restated -- and None, not 0.0, because "no
        dislocation" and "no idea" must not read alike to the caller.
        """
        perp = self._market_data.futures_mid(symbol)
        if perp is None or perp <= 0:
            return None
        try:
            spot = self._market_data.spot(symbol)
        except (ApiError, requests.RequestException) as exc:
            LOG.debug("%s: no spot to measure the basis against: %s",
                      symbol, exc)
            return None
        if spot <= 0:
            return None
        basis = (perp - spot) / spot * 10_000.0
        mean, seen = self._basis_ewma.get(symbol, (basis, 0))
        self._basis_ewma[symbol] = (
            mean + (basis - mean) * BASIS_EWMA_ALPHA, seen + 1)
        if seen < BASIS_EWMA_MIN_SAMPLES:
            return None
        return basis - mean

    def _scalp_signal(self, symbol: str) -> tuple[Side, float, float] | None:
        """
        Which way the perp is going, or None when there is nothing to trade.

        Returns (side, perp move in bps, basis dislocation in bps).

        Two conditions, both required. The first is that the perp has moved
        at all; the second is that spot has not followed it yet, which is
        the half that makes this a lead rather than momentum. Setting
        scalp_min_basis_bps to 0 drops the second and leaves a pure momentum
        strategy -- a real setting, and a different one, so it is opted into
        rather than arrived at.

        The freshness check is per SYMBOL and cannot be delegated to the
        socket's own health. That flag asks whether any frame arrived
        recently across every subscription, so a busy BTC stream keeps it
        green while a quiet market's newest tick is a minute old -- and a
        minute-old tick is not a millisecond lead, it is history.
        """
        cfg = self._cfg
        age = self._market_data.futures_tick_age_ms(symbol)
        if age is None or age > cfg.scalp_max_tick_age_ms:
            return None
        move = self._market_data.futures_move_bps(symbol, cfg.scalp_lookback_ms)
        if move is None or abs(move) < cfg.scalp_min_move_bps:
            return None
        side = Side.UP if move > 0 else Side.DOWN
        if cfg.scalp_min_basis_bps <= 0:
            return side, move, 0.0
        dislocation = self._basis_dislocation_bps(symbol)
        if dislocation is None:
            return None
        # Same sign as the move, by at least the threshold. A perp that has
        # risen while the basis RICHENED means spot is behind; one that has
        # risen while the basis cheapened means spot has already caught up
        # and overtaken, and the thing being chased is over.
        if move > 0 and dislocation < cfg.scalp_min_basis_bps:
            return None
        if move < 0 and dislocation > -cfg.scalp_min_basis_bps:
            return None
        return side, move, dislocation

    def _maybe_enter_scalp(self, bankroll: float, mode: str) -> None:
        """
        Buy the side the perp is moving toward, and bracket it immediately.

        No model, no strike, no volatility, no buffer. This path never asks
        which side wins the round -- it asks which way this token's PRICE is
        about to move, over the next few seconds, and takes a fixed profit or
        a fixed loss either way.

        A round is entered repeatedly, so _seen is deliberately never
        written. Three bounds replace it: a per-round ceiling, a per-symbol
        cooldown, and the requirement that the symbol be completely flat --
        no position and no resting order. Stacking a second entry on a live
        bracket would leave the stop watching a price that neither fill
        chose.
        """
        cfg = self._cfg
        now_ms = self._client.now_ms()
        self._prune(now_ms)
        if len(self._positions) >= cfg.max_concurrent_positions:
            return

        target = bankroll * cfg.scalp_stake_pct
        if min(target, self._available(bankroll)) < cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a scalp (%.2f committed "
                      "of %.2f)", self._committed(), bankroll)
            return

        now_s = time.monotonic()
        for raw in self._client.list_rounds():
            secs = raw.seconds_remaining(now_ms)
            if not (cfg.entry_window_end_s <= secs <= cfg.entry_window_start_s):
                continue
            # Flat, on both counts. A pending order on this round is a
            # bracket leg or an entry still resolving; either way the symbol
            # is not free.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            if any(p.rnd.symbol == raw.symbol for p in self._pending.values()):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue

            end_ms, taken = self._scalp_entries.get(raw.topic_id,
                                                   (raw.end_ms, 0))
            if taken >= cfg.scalp_max_entries_per_round:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the round's scalp ceiling is reached")
                continue
            last = self._scalp_last_entry.get(raw.symbol)
            if last is not None and now_s - last < cfg.scalp_cooldown_s:
                continue

            if cfg.min_liquidity > 0 and (raw.liquidity is None
                                          or raw.liquidity < cfg.min_liquidity):
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the book is thinner than the minimum")
                continue

            # The bracket has to be worth placing before a quote is spent on
            # finding out. This is a property of the market's fee and the two
            # targets, so it can be answered before anything is asked of the
            # venue.
            required = signal_edge_required(raw.fee_bps,
                                            cfg.scalp_take_profit_pct,
                                            cfg.scalp_stop_loss_pct)
            if required is None or required > cfg.scalp_max_edge_required:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the fee demands more signal than the "
                                "bracket can carry")
                LOG.debug("%s: a %d bps fee needs %s hit-rate points over a "
                          "coin flip, above the %.2f ceiling", raw.slug,
                          raw.fee_bps,
                          "no reachable" if required is None
                          else f"{required:.2f}", cfg.scalp_max_edge_required)
                continue

            symbol = self._client.market_symbol(raw.feed_symbol)
            signal = self._scalp_signal(symbol)
            if signal is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the futures feed shows no lead to trade")
                continue
            side, move_bps, dislocation = signal

            stake = min(target, self._available(bankroll))
            if stake < cfg.min_stake_usdt:
                continue

            quote = None
            price = self._book_price(raw, side)
            if price is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the side has no tradable price")
                continue
            if self._live:
                fresh = self._live_bankroll("scalp entry")
                if fresh is None:
                    continue
                stake = min(stake, fresh)
                if stake < cfg.min_stake_usdt:
                    LOG.warning("%s: the wallet holds %.2f, under the %.2f "
                                "minimum order; skipping", raw.slug, fresh,
                                cfg.min_stake_usdt)
                    continue
                quote = self._client.get_quote(raw, _market_buy(side, stake))
                # The quote is authoritative; the book was a screen. Every
                # gate below binds on the price that will actually execute,
                # because the bracket is computed from that price and a
                # bracket built on a screen price protects nothing.
                price = quote.average_price
                if abs(quote.price_impact) > cfg.max_price_impact:
                    LOG.info("%s: price impact %.1f%% too high for a scalp; "
                             "skipping", raw.slug, quote.price_impact * 100)
                    continue
            if not cfg.min_entry_price <= price <= cfg.max_entry_price:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the side is priced outside the band")
                continue

            bracket = bracket_prices(price, raw.fee_bps,
                                     cfg.scalp_take_profit_pct,
                                     cfg.scalp_stop_loss_pct)
            if bracket is None:
                # Usually the take-profit landing at or above 1.00: this fill
                # is too near the top of the book for a 5% gain to exist.
                self._watching[raw.topic_id] = (
                    raw.end_ms, "no bracket fits around the price on offer")
                continue
            tp_price, stop_price = bracket

            self._watching.pop(raw.topic_id, None)
            placed = self._place_leg(raw, side, price, stake, quote)
            if placed is None:
                continue                  # killed by the venue; nothing open
            price, stake, order_id = placed
            # Re-derive from the CONFIRMED fill. _place_leg can come back
            # with a different price and a smaller stake than the screen, and
            # a bracket around the wrong price is the one failure this whole
            # strategy cannot absorb.
            bracket = bracket_prices(price, raw.fee_bps,
                                     cfg.scalp_take_profit_pct,
                                     cfg.scalp_stop_loss_pct)
            if bracket is None:
                LOG.error("%s: filled at %.4f, which no bracket fits; "
                          "closing it straight back out", raw.slug, price)
                tp_price = stop_price = 0.0
            else:
                tp_price, stop_price = bracket

            # model_prob is the MARKET'S implied probability, not a forecast:
            # this path makes none. Recorded so the journal row says what was
            # paid. edge is 0.0 because paying the market price is by
            # definition no edge over it, and every one of these rows closes
            # as settle_source='sold', which diagnose() already keeps out of
            # the calibration buckets.
            sig = Signal(side, model_prob=breakeven_probability(price,
                                                               raw.fee_bps),
                         fill_price=price, edge=0.0, stake_usdt=stake,
                         seconds_left=secs)
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id,
                                       order_type=OrderType.MARKET.value)
            key = (raw.symbol, side)
            self._positions[key] = Position(tid, raw, sig, stake, 1)
            self._scalp_entries[raw.topic_id] = (raw.end_ms, taken + 1)
            self._scalp_last_entry[raw.symbol] = now_s
            LOG.info("SCALP %s %s | in %.4f (%.2f) tp %.4f stop %.4f | perp "
                     "%+.1fbp basis %+.1fbp (%.0fs left, #%d)",
                     raw.slug, side.value, price, stake, tp_price, stop_price,
                     move_bps, dislocation, secs, taken + 1)

            if bracket is None:
                self._sell_now(self._positions[key], "no bracket fits")
            else:
                self._arm_bracket(key, price, tp_price, stop_price)

            if (len(self._positions) >= cfg.max_concurrent_positions
                    or self._available(bankroll) < cfg.min_stake_usdt):
                return

    def _arm_bracket(self, key: tuple[str, Side], entry: float,
                     tp_price: float, stop_price: float) -> None:
        """
        Post the resting take-profit and arm the stop.

        Only the take-profit becomes an order. The stop is a price this bot
        watches, because a SELL limit below the bid is marketable and would
        close the position on the spot instead of waiting -- see Bracket.

        A take-profit the venue refuses is not fatal and is not silent: the
        stop still guards the position, and the flatten deadline still closes
        it. What would be fatal is recording a bracket whose take-profit does
        not exist, so the order id stays None and _check_stops has nothing to
        cancel.
        """
        pos = self._positions.get(key)
        if pos is None:
            return
        tp_price = pos.rnd.round_price(tp_price)
        shares = pos.committed_usdt / max(pos.signal.fill_price, EPS)
        order_id: str | None = None
        if 0.0 < tp_price < 1.0 and shares > EPS:
            plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                             order_type=OrderType.LIMIT, amount=shares,
                             price_limit=tp_price)
            try:
                if self._live:
                    quote = self._client.get_quote(pos.rnd, plan)
                    order_id = str(self._client.place_order(pos.rnd, quote))
                else:
                    order_id = str(self._paper_book.place(plan, pos.rnd))
            except (ApiError, requests.RequestException) as exc:
                LOG.error("%s: the take-profit was refused (%s); the stop and "
                          "the flatten deadline are all that guard this "
                          "position", pos.rnd.slug, exc)
                order_id = None
            if order_id is not None:
                self._pending[order_id] = PendingOrder(
                    order_id=order_id, rnd=pos.rnd, plan=plan,
                    signal=pos.signal,
                    # Round end, not the entry window: a take-profit is not
                    # an entry and has no reason to stop being useful when
                    # entries do. The flatten deadline retracts it first in
                    # the ordinary case; this is the backstop.
                    expires_at_ms=pos.rnd.end_ms,
                    filled_usdt=0.0, filled_shares=0.0,
                    trade_id=pos.trade_id)
        self._brackets[key] = Bracket(entry_price=entry, tp_price=tp_price,
                                      stop_price=stop_price,
                                      tp_order_id=order_id)

    def _check_stops(self) -> None:
        """
        Fire the stop leg on any bracket whose bid has fallen to it.

        Runs after the reaper, so a take-profit that filled this pass has
        already closed its position and taken its bracket with it -- which is
        the "cancel the other one" half of the pair, in the direction where
        there is nothing to cancel.
        """
        if not self._cfg.scalp:
            return
        for key, bracket in list(self._brackets.items()):
            pos = self._positions.get(key)
            if pos is None:
                self._brackets.pop(key, None)
                continue
            bids = self._market_data.bids(pos.rnd, pos.signal.side)
            if not bids:
                continue
            bid = bids[0][0]
            if bid > bracket.stop_price:
                continue
            LOG.info("STOP %s %s | bid %.4f at or under %.4f (entry %.4f, "
                     "take-profit was %.4f)", pos.rnd.slug,
                     pos.signal.side.value, bid, bracket.stop_price,
                     bracket.entry_price, bracket.tp_price)
            if bracket.tp_order_id and not self._retract(
                    bracket.tp_order_id, "stop triggered"):
                # The take-profit could not be reached. Selling now would put
                # more shares on offer than the position holds, and the venue
                # would fill both. Wait a pass; the stop re-fires while the
                # bid stays down.
                continue
            self._brackets.pop(key, None)
            pos = self._positions.get(key)
            if pos is None:
                # The cancel raced the take-profit and lost: it had already
                # filled, and _retract booked it. That is the good ending.
                continue
            self._sell_now(pos, "stop")

    def _flatten_scalps(self) -> None:
        """
        Close everything before the last minute, and place nothing inside it.

        The book thins as a round ends, which is the whole reason the profile
        stops early -- so this fires at the edge of that minute rather than
        inside it, and each round is flattened exactly once. Re-running it
        would cancel the very exit order the first pass placed.
        """
        if not self._cfg.scalp:
            return
        now_ms = self._client.now_ms()
        due = {p.rnd.topic_id for p in self._positions.values()
               if p.rnd.seconds_remaining(now_ms) <= self._cfg.scalp_flatten_s}
        due -= self._flattened
        if not due:
            return
        for order_id, pending in list(self._pending.items()):
            if pending.rnd.topic_id in due:
                self._retract(order_id, "flatten deadline")
        for key, pos in list(self._positions.items()):
            if pos.rnd.topic_id not in due:
                continue
            self._brackets.pop(key, None)
            LOG.info("FLATTEN %s %s | %.4f USDT with %.0fs left",
                     pos.rnd.slug, pos.signal.side.value, pos.committed_usdt,
                     pos.rnd.seconds_remaining(now_ms))
            if not self._sell_now(pos, "flatten"):
                # The honest fallback. Retrying into a book that is not there
                # is how a 5% loss becomes a 100% one; letting the oracle
                # decide is the smaller of the two bad endings, and it is the
                # one the rest of this bot already knows how to finish.
                LOG.error("%s: could not flatten %.4f USDT; it will run to "
                          "settlement as a FULL-STAKE bet, which is not what "
                          "this profile's risk numbers assume",
                          pos.rnd.slug, pos.committed_usdt)
        self._flattened |= due

    def _sell_now(self, pos: Position, reason: str) -> bool:
        """
        Offer the whole position back to the market, to be filled now.

        A MARKETABLE LIMIT, NOT A MARKET ORDER, and the difference matters at
        exactly the moment this is called. A MARKET order here is FOK: on the
        thin book that triggered the stop it fills entirely or not at all, and
        "not at all" is a position that was supposed to be capped and is now
        running to settlement. A limit priced through the bid sweeps whatever
        depth exists, keeps the partial fill, and rests the remainder where
        the reaper will retract it.

        The price is the best bid less the profile's own slippage cap, so how
        far this is willing to reach through the book is the number that
        already governs how far every other order may reach.

        True when an order went out. False means the position is still open
        and nothing is protecting it, which every caller reports loudly.
        """
        bids = self._market_data.bids(pos.rnd, pos.signal.side)
        if not bids:
            LOG.error("%s: nothing is bidding for %s; cannot sell",
                      pos.rnd.slug, pos.signal.side.value)
            return False
        floor = bids[0][0] * (1.0 - self._cfg.max_slippage_bps / 10_000.0)
        price = pos.rnd.round_price(floor)
        if not 0.0 < price < 1.0:
            LOG.error("%s: a sale through the bid prices at %.4f, which is "
                      "not tradable", pos.rnd.slug, price)
            return False
        shares = pos.committed_usdt / max(pos.signal.fill_price, EPS)
        if shares <= EPS:
            return False
        plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                         order_type=OrderType.LIMIT, amount=shares,
                         price_limit=price)
        try:
            if self._live:
                quote = self._client.get_quote(pos.rnd, plan)
                order_id = str(self._client.place_order(pos.rnd, quote))
            else:
                order_id = str(self._paper_book.place(plan, pos.rnd))
        except (ApiError, requests.RequestException) as exc:
            LOG.error("%s: the %s sale was refused: %s", pos.rnd.slug,
                      reason, exc)
            return False
        self._pending[order_id] = PendingOrder(
            order_id=order_id, rnd=pos.rnd, plan=plan, signal=pos.signal,
            expires_at_ms=pos.rnd.end_ms, filled_usdt=0.0, filled_shares=0.0,
            trade_id=pos.trade_id)
        LOG.info("SELL %s %s | %.4f shares through the bid at %.4f (%s)",
                 pos.rnd.slug, pos.signal.side.value, shares, price, reason)
        return True

    def _maybe_scale_in_all(self, bankroll: float) -> None:
        if self._cfg.last_minute or self._cfg.scalp:
            # last_minute: one order, one round, held to settlement.
            # scalp: a top-up would move the fill price the bracket was
            # already computed from, leaving the stop guarding a level
            # neither tranche chose.
            #
            # Config rejects both pairings outright; this is the belt to that
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
        spot = self._market_data.spot(symbol)
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

        levels = self._market_data.asks(pos.rnd, pos.signal.side)
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
            quote = self._client.get_quote(pos.rnd, _market_buy(pos.signal.side, topup))
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

        WHY THIS ONLY REPORTS, AND DOES NOT BOOK
        ----------------------------------------
        It briefly did book the gap against PnL, on the reasoning that two
        balance reads seconds apart cannot bracket a deposit. The reads are
        that close together; they are not that clean, and the arithmetic here
        was wrong in a way that cost real money.

        A WIN is the fatal case. Its payout lands minutes later, so `actual`
        is near zero here while `expected_move` is the whole gross payout,
        and the only thing standing between that and a correction of MINUS
        THE ENTIRE PAYOUT was a guard requiring the balance to be unchanged
        to the last EPS. Anything at all moving in the window defeats it --
        an earlier claim landing, dust, or the other leg of the very same
        straddle round settling a moment before, which is not an edge case
        but the normal shape of every straddle. A won round could book its
        payout into PnL and then immediately subtract it again.

        A LOSS is only better by degree: expected_move is zero, so any
        unrelated credit landing inside the window reads as this round's
        modelling error.

        The window is too narrow for a deposit and still too wide for
        attribution, which leaves this measurement useful as a SIGNAL and
        unfit as a LEDGER ENTRY. So it warns, and the books are squared where
        the measurement is unambiguous instead: RiskManager.reconcile, which
        runs only with a flat book and nothing in flight, and which anchors
        the baseline to the venue's balance so that no residue survives to
        accumulate. The venue is still the single source of truth -- this is
        just not the place that reads it.
        """
        actual = after - before
        # A loss should move nothing: the money went out when the order did.
        expected_move = (pos.committed_usdt / max(pos.signal.fill_price, EPS)
                         if won else 0.0)
        reference = max(abs(expected_move), self._cfg.min_stake_usdt)
        drift = abs(actual - expected_move)

        if won:
            # The claim is queued, not credited. There is nothing to compare
            # against yet and there will not be before this returns.
            LOG.debug("Settled a win on %s; payout %.4f not yet credited, "
                      "balance moved %+.4f", pos.rnd.slug, expected_move,
                      actual)
            return
        if drift <= reference * self._cfg.reconcile_tolerance:
            LOG.debug("Reconciled a loss: expected the balance to move "
                      "%+.4f, it moved %+.4f (P&L %+.4f)",
                      expected_move, actual, expected_pnl)
            return

        LOG.warning(
            "RECONCILE MISMATCH on %s: settling a loss should have moved the "
            "balance %+.4f, it moved %+.4f (gap %.4f, P&L %+.4f). Either a "
            "cost is unmodelled here or something landed in the window. Not "
            "booked -- the balance is squared against the venue when the book "
            "next goes flat.",
            pos.rnd.slug, expected_move, actual, drift, expected_pnl)

    def _drain(self, timeout_s: float | None = None) -> None:
        # Cancel first, then wait. A resting order left behind on shutdown is
        # live money with nothing tracking it: the process that placed it is
        # gone, so nothing will settle it, claim it, or even record that it
        # exists. Render sends SIGTERM on every deploy, so this is the
        # ordinary path and not the exceptional one.
        self._cancel_all_pending()
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

    md = ws_feeds.MarketData(client, cfg)
    md.start()
    # A moment for the handshakes. Not a health gate: REST-only is a
    # supported running mode, and PREFLIGHT_REQUIRED=1 must not start
    # refusing boots because an accelerator was slow.
    time.sleep(2.0)
    feeds = md.status()
    # futures is "off" unless the scalp strategy is selected -- and for that
    # strategy it is the one feed whose absence means no trading at all.
    check("websocket feeds",
          lambda: f"spot {feeds['spot']}, book {feeds['book']}, "
                  f"futures {feeds['futures']}")
    md.stop()
    def vol_check() -> str:
        est = VolatilityEstimator(cfg, ws_feeds.MarketData(client, cfg))
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
                f"avg {client.get_quote(hydrated[0], _market_buy(Side.UP, cfg.min_stake_usdt)).average_price:.4f}"))
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
            q = client.get_quote(rnd, _market_buy(Side.UP, amount))
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
