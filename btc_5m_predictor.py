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
import hashlib
import hmac
import dataclasses
import json
import logging
import math
import os
import signal
import sqlite3
import statistics
import sys
import time
import re
import urllib.parse
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Iterable, Optional

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
    live: bool = False

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
    symbols: tuple[str, ...] = ("BTCUSDT",)
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

    # --- Tolerances and paging (previously hardcoded) -------------------
    round_duration_tolerance: float = 0.10    # fraction of the target length
    quote_consistency_tolerance: float = 0.10
    market_list_limit: int = 50
    # 50 could silently miss an older settlement and leave a position
    # looking unresolved when the venue had already settled it.
    settled_history_limit: int = 200

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
        if not 0 < self.assumed_spread_pct < 1.0:
            raise ValueError("assumed_spread_pct must be in (0, 1)")
        if not self.symbols:
            raise ValueError("symbols must not be empty")
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
                     "vol_cache_s", "error_backoff_max_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
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

    @property
    def symbol(self) -> str:
        """The first configured market. Used where a single default is needed."""
        return self.symbols[0]

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
    "convex": dict(max_entry_price=0.35, min_entry_price=0.05,
                   min_edge=0.02, min_edge_ratio=0.30,
                   max_stake_pct=0.02, entry_window_start_s=280,
                   entry_window_end_s=30, max_consecutive_losses=60,
                   # Longshots: many small losses, so the default fits.
                   daily_loss_limit_pct=0.20, assumed_spread_pct=0.10,
                   # Tail probabilities are the least reliable part of the
                   # model, and this profile lives on them, so size more
                   # cautiously than a mid-price strategy would.
                   kelly_fraction=0.20,
                   min_liquidity=0.0, max_rounds_per_day=200,
                   paper_start_bankroll=100.0,
                   # Inert here unless scale_in is enabled; sized to this
                   # profile's own band (0.05-0.35), not buffer's.
                   max_blended_price=0.3),
    # Symmetric: trades anywhere it finds an edge. Higher hit rate, smaller
    # payoffs, and correspondingly larger individual losses.
    "balanced": dict(max_entry_price=0.90, min_entry_price=0.10,
                     min_edge=0.04, min_edge_ratio=0.10,
                     max_stake_pct=0.05, entry_window_start_s=150,
                     entry_window_end_s=25, max_consecutive_losses=10,
                     daily_loss_limit_pct=0.20, assumed_spread_pct=0.06,
                   kelly_fraction=0.25,
                     min_liquidity=0.0, max_rounds_per_day=200,
                     paper_start_bankroll=100.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.10-0.90), not buffer's.
                     max_blended_price=0.8),
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
    "favorite": dict(max_entry_price=0.80, min_entry_price=0.55,
                     min_edge=0.03, min_edge_ratio=0.05,
                     max_stake_pct=0.10, min_stake_usdt=1.0,
                     daily_loss_limit_pct=0.30, assumed_spread_pct=0.04,
                   kelly_fraction=0.25,
                     min_liquidity=0.0, max_rounds_per_day=200,
                     entry_window_start_s=120, entry_window_end_s=20,
                     max_consecutive_losses=10, paper_start_bankroll=25.0,
                     # Inert here unless scale_in is enabled; sized to this
                     # profile's own band (0.55-0.80), not buffer's.
                     max_blended_price=0.72),
    # YOUR METHOD, encoded. Wait for a large buffer late in the round, then
    # size up. Trades the 0.80-0.97 band, which every other profile refuses.
    #
    # The payoff shape is deliberately many small wins with rare large losses.
    # That is only sound because sizing is by edge, not by payout: at an ask
    # of 0.95 a true 0.99 is worth 0.80 of full Kelly, while a true 0.94 is
    # NEGATIVE. A three-point model error at this end flips the sign, so this
    # profile lives or dies on calibration in its top bucket -- check that
    # bucket in --calibration-report before trusting it with size.
    #
    # min_buffer_sigmas=2.0 means spot must sit two standard deviations of
    # the REMAINING time away from the strike: roughly 14 bps with a minute
    # left, or 28 bps with four minutes.
    "buffer": dict(max_entry_price=0.97, min_entry_price=0.80,
                   min_edge=0.012, min_edge_ratio=0.010,
                   # Lowered from 2.0: fewer sigmas means more trades, and
                   # more trades is how the edge question gets answered at
                   # all -- separating a 68% win rate from a 72% one needs
                   # hundreds of samples. Each trade carries less edge, so
                   # this is a deliberate trade of quality for sample size.
                   min_buffer_sigmas=1.5,
                   max_stake_pct=0.10, min_stake_usdt=1.0,
                   # A loss here costs a full 10% of bankroll, so the 20%
                   # default halted the day after TWO losses -- on 80% of
                   # days at an 85% win rate. The limit has to match the
                   # profile's own loss shape or it stops a healthy bot.
                   daily_loss_limit_pct=0.35,
                   # Near-certainties late in a round sit in thin books;
                   # a wide fill destroys an edge measured in single points.
                   max_price_impact=0.02,
                   assumed_spread_pct=0.03,
                   kelly_fraction=0.25,
                   # Near-certainties late in a round sit in thin books, and
                   # this is the one profile where an empty book is common.
                   min_liquidity=1000.0, max_rounds_per_day=250,
                   # A smaller first tranche leaves more room to add once the
                   # round has proven itself, so the top-up genuinely is the
                   # larger bet -- roughly 4x the opener rather than 1.5x.
                   # Total exposure is still bounded by Kelly, and by the
                   # blended-price cap below.
                   scale_in=True, scale_in_initial_pct=0.25,
                   scale_in_min_topup=1.0,
                   # Never let the blend past 0.90: about nine wins per loss.
                   # Raise it for a higher hit rate and smaller wins; lower it
                   # for bigger wins and fewer of them.
                   max_blended_price=0.90,
                   entry_window_start_s=180, entry_window_end_s=15,
                   max_consecutive_losses=6, paper_start_bankroll=25.0),
    "micro": dict(max_entry_price=0.75, min_entry_price=0.35,
                  min_edge=0.03, min_edge_ratio=0.06,
                  max_stake_pct=0.20, min_stake_usdt=1.0,
                  # 20% per trade means the 20% default halted after ONE loss.
                  # Even 45% halts after 2.25. At this stake fraction the
                  # stake cap and the daily limit are in genuine tension --
                  # that tension is forced by a 1.00 order minimum on a ~7
                  # balance, not chosen. Trading a bigger account is the only
                  # real fix; 55% is the least-bad compromise.
                  daily_loss_limit_pct=0.55,
                  assumed_spread_pct=0.05,
                  # A tiny balance forces a large stake fraction, so cap the
                  # hard ceiling tightly and let Kelly stay conservative.
                   kelly_fraction=0.25,
                  min_liquidity=0.0, max_rounds_per_day=200,
                  entry_window_start_s=200, entry_window_end_s=25,
                  max_consecutive_losses=10, paper_start_bankroll=7.0,
                  # Inert here unless scale_in is enabled; sized to this
                  # profile's own band (0.35-0.75), not buffer's.
                  max_blended_price=0.65),
}


# --------------------------------------------------------------------------
# Configuration file and hot reload
# --------------------------------------------------------------------------

# The default strategy, declared once. Previously six literals across four
# files each carried their own copy of this, which is precisely how a default
# drifts: change five and the sixth silently disagrees.
DEFAULT_PROFILE = "buffer"

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
                 live: Optional[bool], db_path: str,
                 profile: Optional[str] = None,
                 overrides: Optional[dict] = None) -> Config:
    """
    Assemble a Config from a configuration document.

    Layering is explicit and one-directional: defaults, then the selected
    profile, then the document's overrides, then any command-line overrides.
    Everything passes through a single mapping so a key can never be supplied
    twice.
    """
    if not isinstance(document, dict):
        raise ValueError("configuration must be a JSON object")

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

    def __init__(self, path: Optional[str], *, api_key: str, api_secret: str,
                 live: Optional[bool], db_path: str,
                 profile: Optional[str] = None,
                 overrides: Optional[dict] = None) -> None:
        self._path = path
        self._identity = dict(api_key=api_key, api_secret=api_secret,
                              live=live, db_path=db_path, profile=profile,
                              overrides=overrides or {})
        self._mtime: Optional[float] = None
        self._document = self._read()
        self._current = build_config(self._document, **self._as_kwargs())
        self.reload_count = 0

    def _as_kwargs(self) -> dict:
        d = dict(self._identity)
        return dict(api_key=d["api_key"], api_secret=d["api_secret"],
                    live=d["live"], db_path=d["db_path"],
                    profile=d["profile"], overrides=d["overrides"])

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
    def path(self) -> Optional[str]:
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
            ignored = ignored | {"live (pinned by --live/--paper or "
                                 "TRADING_MODE)"}
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
    liquidity: Optional[float]
    strike: Optional[float] = None      # variantData.startPrice
    feed_symbol: Optional[str] = None   # oracle the venue resolves against

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
class Signal:
    side: Side
    model_prob: float
    fill_price: float
    edge: float
    stake_usdt: float
    seconds_left: float
    buffer_z: float = 0.0


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

    def __init__(self, message: str, code: Optional[int] = None,
                 status: Optional[int] = None) -> None:
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


def _as_float_or_none(value: object) -> Optional[float]:
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
                           tail_df: Optional[float] = None) -> float:
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


def kelly_stake(bankroll: float, model_prob: float, price: float,
                cfg: Config, fee_bps: Optional[int] = None) -> float:
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
                   price: float, fee_bps: int) -> Optional[float]:
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
              ) -> Optional[float]:
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


class VolatilityEstimator:
    """Annualised sigma AND tail thickness from recent 1m returns."""

    def __init__(self, cfg: Config | "ConfigStore",
                 session: requests.Session) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._session = session
        self._cache: dict[str, tuple[float, float]] = {}
        self._df_cache: dict[str, Optional[float]] = {}
        self._clamped: dict[str, bool] = {}
        self._raw: dict[str, float] = {}

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    def sigma_annual(self, symbol: Optional[str] = None) -> float:
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
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])
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
        self._cache[symbol] = (annual, time.time())
        return annual

    def _estimate_df(self, rets: list[float], sd: float) -> Optional[float]:
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

    def tail_df(self, symbol: Optional[str] = None) -> Optional[float]:
        """Tail parameter for `symbol`. Call sigma_annual first."""
        return self._df_cache.get(symbol or self._cfg.symbol)

    def raw_sigma(self, symbol: Optional[str] = None) -> Optional[float]:
        """Measured sigma before clamping, for diagnostics."""
        return self._raw.get(symbol or self._cfg.symbol)

    def is_clamped(self, symbol: Optional[str] = None) -> bool:
        """True if the last sigma hit a bound and is therefore not a measurement."""
        return self._clamped.get(symbol or self._cfg.symbol, False)


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


class RiskManager:
    """Owns every reason to stop. Fails closed on all of them."""

    def __init__(self, cfg: Config | "ConfigStore",
                 starting_bankroll: float) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._day_start_bankroll = max(starting_bankroll, EPS)
        self._day_key = time.strftime("%Y-%m-%d")
        self.consecutive_losses = 0
        self.rounds_today = 0
        self.halted_reason: Optional[str] = None
        # Poisson-binomial accumulators: each trade contributes its own model
        # probability, so expectation is well defined even though every trade
        # has different odds.
        self._expected_wins = 0.0
        self._variance = 0.0
        self._actual_wins = 0
        self._samples = 0

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    def calibration_z(self) -> Optional[float]:
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
            LOG.info("New trading day; baseline bankroll %.2f", bankroll)

    def check(self, bankroll: float) -> None:
        self._roll_day(bankroll)
        if self.halted_reason:
            raise TradingHalted(self.halted_reason)

        drawdown = 1.0 - (bankroll / self._day_start_bankroll)
        if drawdown >= self._cfg.daily_loss_limit_pct:
            self._halt(f"daily loss limit: {drawdown:.1%} down "
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
                      model_prob: Optional[float] = None) -> None:
        self.rounds_today += 1
        self.consecutive_losses = 0 if won else self.consecutive_losses + 1
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
    symbols: tuple[str, ...] = ("BTCUSDT",)

    def __init__(self, cfg: Config | "ConfigStore") -> None:
        # Accepts either a Config or a ConfigStore. With a store, `_cfg`
        # resolves to the current configuration on every access, so a hot
        # reload takes effect without rebuilding the client or its session.
        self._store: Optional["ConfigStore"] = None
        self._static_cfg: Optional[Config] = None
        if isinstance(cfg, Config):
            self._static_cfg = cfg
        else:
            self._store = cfg
        cfg = self._cfg
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": cfg.api_key})
        self._clock_offset_ms = 0
        self._wallet: Optional[WalletRef] = None
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
    _ERROR_HINTS = {
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
    def _json_or_none(response) -> Optional[object]:
        """Parsed JSON, or None when the body is not JSON at all."""
        try:
            return response.json()
        except ValueError:
            return None

    def _request(self, name: str, params: Optional[dict] = None) -> dict:
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
            code_int: Optional[int] = None
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

    def market_symbol(self, feed_symbol: Optional[str]) -> str:
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

    def spot_price(self, symbol: Optional[str] = None) -> float:
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

    def prediction_wallet_value(self) -> Optional[float]:
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

    def funding_plan(self) -> tuple[str, str, Optional[str]]:
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

    def remaining_quota_usdt(self) -> Optional[float]:
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
        # only the configured symbols.
        out = []
        for topic in payload.get("marketTopics") or []:
            rnd = self._parse_round(topic)
            if rnd is not None:
                out.append(rnd)
        return out

    def market_detail(self, topic_id: int) -> dict:
        return self._request("market_detail",
                             {"marketTopicId": topic_id})

    def hydrate(self, rnd: Round) -> Optional[Round]:
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
    def _parse_variant(vd: dict) -> tuple[Optional[float], Optional[str]]:
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
    def _parse_round(topic: dict) -> Optional[Round]:
        """
        Validate untrusted payload once, into a precise type.

        Returns None for anything that is not a live BTC 5m up/down market.
        Downstream code may assume every Round is well-formed. `strike` is left
        None here: the list response often omits variantData, and requiring it
        would reject every round and leave the bot silently never trading.
        """
        try:
            if topic.get("chartType") != "CRYPTO_UP_DOWN":
                return None
            if topic.get("symbol") not in PredictionClient.symbols:
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

            strike, symbol = PredictionClient._parse_variant(
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
                symbol=str(topic.get("symbol")),
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
                strike=strike, feed_symbol=symbol)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # OverflowError is NOT a ValueError: int(float("inf")) raises it,
            # and "1e999" parses to inf before reaching int().
            LOG.warning("Skipping malformed market payload: %s", exc)
            return None

    def asks_for(self, rnd: Round, side: Side
                 ) -> Optional[list[tuple[float, float]]]:
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
    def _parse_asks(payload: dict) -> Optional[list[tuple[float, float]]]:
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
                           low: float = 0.25, high: Optional[float] = None,
                           tolerance: float = 0.05) -> Optional[float]:
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
                    stake_usdt: Optional[float] = None) -> str:
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

    def order_fill(self, order_id: str) -> Optional[dict]:
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
                    raise ApiError(
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
                        ) -> Optional[tuple[Side, Optional[float]]]:
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
        if not hashes and payload.get("batchId"):
            LOG.info("Redemption batch %s accepted, no tx hash yet",
                     payload["batchId"])
        return hashes

    def redeem_status(self, tx_hash: str) -> Optional[str]:
        """Status of a redemption transaction, or None if unknown."""
        try:
            payload = self._request("redeem_status", {
                "walletAddress": self.wallet().address, "txHash": tx_hash})
        except ApiError as exc:
            LOG.debug("Redeem-status lookup failed for %s: %s", tx_hash, exc)
            return None
        status = payload.get("status")
        return None if status is None else str(status).upper()

    def final_price(self, rnd: Round) -> Optional[float]:
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
                symbol TEXT,
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
        self._conn.commit()

    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (ts, mode, slug, topic_id, side, strike, spot,"
            " sigma, seconds_left, end_ms, model_prob, fill_price, edge, stake,"
            " bankroll_before, order_id, profile, buffer_z, fee_bps, symbol)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mode, rnd.slug, rnd.topic_id, sig.side.value,
             rnd.strike, spot, sigma, sig.seconds_left, rnd.end_ms,
             sig.model_prob, sig.fill_price, sig.edge, sig.stake_usdt,
             bankroll, order_id, self._profile, sig.buffer_z, rnd.fee_bps,
             rnd.symbol))
        self._conn.commit()
        return int(cur.lastrowid)

    def resolve(self, trade_id: int, won: bool, pnl: float,
                source: str) -> None:
        self._conn.execute(
            "UPDATE trades SET resolved=1, won=?, pnl=?, settle_source=?"
            " WHERE id=?", (1 if won else 0, pnl, source, trade_id))
        self._conn.commit()

    def diagnose(self, profile: Optional[str] = None,
                 symbol: Optional[str] = None) -> str:
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
                f"Inconclusive: {len(unclear)} band(s) lack the sample to call,"
                f" {len(winning)} show edge, {len(losing)} show none.",
                "",
                "Over a few dozen trades a 68% win rate and a 76% win rate are",
                "indistinguishable, yet one loses money and the other compounds.",
                "Keep the settings fixed and let the sample grow. Changing size",
                "in response to a losing streak is the one move that converts",
                "an unclear result into a certain loss.",
            ]
        return "\n".join(out)

    def calibration_report(self, profile: Optional[str] = None) -> str:
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
            parts = [f"Journal contains {len(profiles)} profiles: "
                     f"{', '.join(profiles)}", ""]
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
                fee_bps: Optional[int] = None) -> bool:
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


def evaluate(rnd: Round, spot: float, sigma: float, bankroll: float,
             now_ms: int, cfg: Config,
             ask_book: Optional[dict[Side, list[tuple[float, float]]]] = None,
             tail_df: Optional[float] = None) -> Optional[Signal]:
    """
    Decide whether this round is worth a trade. Pure: no I/O, no mutation.

    Two-pass sizing: size off top-of-book, re-price that stake against the real
    ladder, then re-check the edge at the true average fill. Sizing off a price
    you will not actually get is how a backtest-positive strategy loses money
    live. In live mode this is a screen; the venue quote is authoritative.

    Returns None when nothing clears the edge, price and risk filters.
    """
    if rnd.strike is None:
        return None

    secs = rnd.seconds_remaining(now_ms)
    if not (cfg.entry_window_end_s <= secs <= cfg.entry_window_start_s):
        return None

    fee_bps = rnd.fee_bps        # the market's published rate, not an assumption
    z = buffer_sigmas(spot, rnd.strike, sigma, secs)
    if cfg.min_buffer_sigmas > 0 and abs(z) < cfg.min_buffer_sigmas:
        return None                  # not enough buffer for the time left
    p_up = digital_up_probability(spot, rnd.strike, sigma, secs, tail_df)

    best: Optional[Signal] = None
    for side, model_prob in ((Side.UP, p_up), (Side.DOWN, 1.0 - p_up)):
        # With a buffer gate, only back the side the buffer actually favours;
        # betting against a large buffer is the opposite of the rule.
        if cfg.min_buffer_sigmas > 0:
            if (side is Side.UP and z < 0) or (side is Side.DOWN and z > 0):
                continue
        levels = (ask_book or {}).get(side)
        entry = (levels[0][0] if levels
                 else min(rnd.quote_for(side) * (1.0 + cfg.assumed_spread_pct),
                          0.999))

        if not (cfg.min_entry_price <= entry <= cfg.max_entry_price):
            continue
        if not clears_edge(model_prob, entry, cfg, fee_bps):
            continue

        stake = kelly_stake(bankroll, model_prob, entry, cfg, fee_bps)
        if stake <= 0:
            continue

        avg = walk_book(levels, stake) if levels else entry
        if avg is None:
            continue
        if not (cfg.min_entry_price <= avg <= cfg.max_entry_price):
            continue

        if not clears_edge(model_prob, avg, cfg, fee_bps):
            continue
        # The venue quotes to its own precision, so a fill price carrying
        # more digits than that is fiction. Snap before pricing the edge.
        avg = rnd.round_price(avg)
        if not 0.0 < avg < 1.0:
            continue
        edge = model_prob - breakeven_probability(avg, fee_bps)

        stake = kelly_stake(bankroll, model_prob, avg, cfg, fee_bps)
        if stake <= 0:
            continue

        cand = Signal(side, model_prob, avg, edge, stake, secs, z)
        if best is None or cand.edge > best.edge:
            best = cand

    return best


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
    return stake * (1.0 - fill_price) / fill_price * (1.0 - fee_bps / 10_000.0)


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

    def __init__(self, cfg: Config | "ConfigStore") -> None:
        self._store: Optional["ConfigStore"] = None
        self._static_cfg: Optional[Config] = None
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
        self._account_risk: Optional[RiskManager] = None
        self._seen: dict[int, int] = {}
        self._positions: dict[str, Position] = {}
        self._hydrated: dict[int, Round] = {}
        self._errors = 0
        # token_id -> (expected payout USDT, tx hashes). Counted toward the
        # bankroll so an unclaimed win is not misread as a drawdown.
        self._unredeemed: dict[str, tuple[float, list[str], str]] = {}
        self._stopping = False
        self._settled_count = 0
        # The mode actually in force. Diverges from the config only while a
        # position is open and a switch is pending.
        self._active_live = config.live

    @staticmethod
    def _resolve(cfg: Config | "ConfigStore") -> Config:
        return cfg if isinstance(cfg, Config) else cfg.current

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @property
    def _position(self) -> Optional[Position]:
        """The single open position, when exactly one market is configured."""
        if len(self._positions) == 1:
            return next(iter(self._positions.values()))
        return None

    @_position.setter
    def _position(self, value: Optional[Position]) -> None:
        if value is None:
            self._positions.clear()
        else:
            self._positions[value.rnd.symbol] = value

    def _risk_for(self, symbol: str) -> RiskManager:
        """Streak and calibration state for one market, created on demand."""
        if symbol not in self._risk:
            source = self._store or self._cfg
            self._risk[symbol] = RiskManager(source, self._bankroll())
        return self._risk[symbol]

    def _committed(self) -> float:
        return sum(p.committed_usdt for p in self._positions.values())

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
        pending = sum(v for v, _, _ in self._unredeemed.values())
        return balance + pending

    def _poll_redemptions(self) -> None:
        """Drop entries once the chain confirms the payout has landed."""
        for token_id, (value, hashes, _chain) in list(self._unredeemed.items()):
            if not hashes:
                continue
            done = [self._client.redeem_status(h) in ("SUCCESS", "CONFIRMED",
                                                      "COMPLETED")
                    for h in hashes]
            if done and all(done):
                self._unredeemed.pop(token_id, None)
                LOG.info("Redemption confirmed: %.2f USDT credited", value)

    def _claim(self, pos: Position) -> None:
        """Redeem a winning position and track it until it is credited."""
        token_id = pos.rnd.token_for(pos.signal.side)
        payout = pos.signal.stake_usdt / pos.signal.fill_price
        try:
            hashes = self._client.batch_redeem([token_id], pos.rnd.chain_id)
            self._unredeemed[token_id] = (payout, hashes, pos.rnd.chain_id)
            LOG.info("Redeeming %.2f USDT (tx %s)", payout,
                     ", ".join(hashes) or "pending")
        except (ApiError, requests.RequestException) as exc:
            # Keep it tracked anyway: the win is real even if the claim failed,
            # and retry happens on the next sweep.
            self._unredeemed[token_id] = (payout, [], pos.rnd.chain_id)
            LOG.warning("Redemption failed for %s (will retry): %s",
                        token_id, exc)

    def _retry_failed_claims(self) -> None:
        for token_id, (value, hashes, chain_id) in list(self._unredeemed.items()):
            if hashes:
                continue
            try:
                new = self._client.batch_redeem([token_id], chain_id)
                if new:
                    self._unredeemed[token_id] = (value, new, chain_id)
                    LOG.info("Redemption retry accepted for %s", token_id)
            except (ApiError, requests.RequestException) as exc:
                LOG.debug("Redemption retry for %s still failing: %s",
                          token_id, exc)

    def _prune(self, now_ms: int) -> None:
        for tid in [t for t, end in self._seen.items()
                    if end < now_ms - self._cfg.prune_after_s * 1000]:
            self._seen.pop(tid, None)
            self._hydrated.pop(tid, None)

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
                 "LIVE" if self._live else "PAPER", bankroll,
                 ", ".join(self._cfg.symbols))
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

                    if self._live and self._unredeemed:
                        self._poll_redemptions()
                        self._retry_failed_claims()
                    self._settle_open()
                    bankroll = self._bankroll()
                    # Account-level limits: one balance, one daily loss cap.
                    # Per-market streaks are checked inside _maybe_enter.
                    self._account_risk.check(bankroll)
                    self._apply_pending_mode()
                    self._maybe_scale_in_all(bankroll)
                    self._maybe_enter(bankroll,
                                      "LIVE" if self._live else "PAPER")
                    self._errors = 0
                except (TradingHalted, Shutdown):
                    raise
                except (ApiError, requests.RequestException) as exc:
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

    def _maybe_enter(self, bankroll: float, mode: str) -> None:
        # Enforced here, not only at the call site: silently replacing an open
        # position would orphan it in the journal and double real exposure.
        if self._position is not None:
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
            if raw.symbol in self._positions:
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue
            if not (self._cfg.entry_window_end_s
                    <= raw.seconds_remaining(now_ms)
                    <= self._cfg.entry_window_start_s):
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
            book = {}
            for side in Side:
                levels = self._client.asks_for(rnd, side)
                if levels:
                    book[side] = levels

            # Size against uncommitted funds, never the full balance.
            sig = evaluate(rnd, spot, sigma, available, now_ms, self._cfg,
                           book or None, tail_df)
            if sig is None:
                continue

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
                     "stake %.2f%s (%.0fs left)", rnd.slug, sig.side.value,
                     sig.fill_price, sig.model_prob, sig.edge,
                     sig.stake_usdt, mult_s, sig.seconds_left)

            tid = self._journal.record(mode, rnd, sig, spot, sigma, bankroll,
                                       order_id)
            self._seen[rnd.topic_id] = rnd.end_ms
            self._positions[rnd.symbol] = Position(tid, rnd, sig,
                                                   sig.stake_usdt, 1)
            available -= sig.stake_usdt
            if (len(self._positions) >= self._cfg.max_concurrent_positions
                    or available < self._cfg.min_stake_usdt):
                return

    def _maybe_scale_in_all(self, bankroll: float) -> None:
        for symbol in list(self._positions):
            self._maybe_scale_in(bankroll, symbol)

    def _maybe_scale_in(self, bankroll: float,
                        symbol: Optional[str] = None) -> None:
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
        pos = (self._positions.get(symbol) if symbol is not None
               else self._position)
        if pos is None or not self._cfg.scale_in:
            return
        symbol = pos.rnd.symbol
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
        if not clears_edge(prob, price, self._cfg, pos.rnd.fee_bps):
            return

        target = kelly_stake(bankroll + pos.committed_usdt, prob, price,
                             self._cfg, pos.rnd.fee_bps)
        topup = target - pos.committed_usdt
        floor = max(self._cfg.scale_in_min_topup, self._cfg.min_stake_usdt)
        if topup < floor:
            return

        # Trim so the blended fill stays under the ceiling. Without this a
        # top-up at a high price silently converts a position that needed six
        # wins per loss into one needing fifteen.
        allowed = max_topup_within_blend(
            pos.committed_usdt, pos.signal.fill_price, price,
            self._cfg.max_blended_price)
        if allowed < floor:
            LOG.debug("No top-up for %s: blended price would exceed %.2f",
                      pos.rnd.slug, self._cfg.max_blended_price)
            return
        if topup > allowed:
            LOG.info("Trimming top-up %.2f -> %.2f to hold the blended price "
                     "under %.2f", topup, allowed,
                     self._cfg.max_blended_price)
            topup = allowed
        avg = walk_book(levels, topup)
        if avg is None or not clears_edge(prob, avg, self._cfg, pos.rnd.fee_bps):
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

        self._positions[symbol] = replace(
            pos,
            signal=replace(pos.signal, model_prob=prob, fill_price=blended,
                           stake_usdt=pos.committed_usdt + topup),
            committed_usdt=pos.committed_usdt + topup,
            tranches=pos.tranches + 1)

    def _live_bankroll(self, context: str) -> Optional[float]:
        """Freshly read tradable balance, or None if it cannot be read."""
        try:
            return self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.warning("Could not confirm the balance before %s: %s",
                        context, exc)
            return None

    def _resize(self, sig: Signal, rnd: Round,
                bankroll: float) -> Optional[Signal]:
        """Re-derive the stake against a balance that has since changed."""
        stake = kelly_stake(bankroll, sig.model_prob, sig.fill_price,
                            self._cfg, rnd.fee_bps)
        if stake <= 0:
            LOG.info("Balance fell to %.2f; no stake clears the limits now",
                     bankroll)
            return None
        if self._cfg.scale_in:
            first = max(stake * self._cfg.scale_in_initial_pct,
                        self._cfg.min_stake_usdt)
            stake = min(first, stake)
        if abs(stake - sig.stake_usdt) > EPS:
            LOG.info("Resized %.2f -> %.2f against a live balance of %.2f",
                     sig.stake_usdt, stake, bankroll)
        return replace(sig, stake_usdt=stake)

    def _settle_open(self) -> None:
        for symbol in list(self._positions):
            self._settle_one(symbol)

    def _settle_one(self, symbol: str) -> None:
        pos = self._positions.get(symbol)
        if pos is None:
            return
        now_ms = self._client.now_ms()
        if now_ms < pos.rnd.end_ms + self._cfg.settle_grace_s * 1000:
            return

        winner: Optional[Side] = None
        pnl: Optional[float] = None
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
                self._position = None
            return

        won = winner is pos.signal.side
        if pnl is None:
            pnl = settle_pnl(max(pos.committed_usdt, pos.signal.stake_usdt),
                             pos.signal.fill_price, won, pos.rnd.fee_bps)
        if not self._live:
            self._paper_bankroll += pnl

        # Read the balance before claiming so the change can be reconciled
        # against what we expected, rather than inferred.
        before: Optional[float] = None
        if self._live:
            before = self._live_bankroll("settlement")

        if won and self._live:
            self._claim(pos)

        self._journal.resolve(pos.trade_id, won, pnl, source)
        self._risk_for(symbol).record_result(won, pos.signal.model_prob)
        if self._account_risk is not None:
            self._account_risk.record_result(won, pos.signal.model_prob)
        self._positions.pop(symbol, None)
        after = self._bankroll()
        LOG.info("SETTLED %s -> %s  P&L %+.2f  bankroll %.2f  [%s]",
                 pos.rnd.slug, "WIN" if won else "LOSS", pnl, after, source)
        if before is not None:
            self._reconcile(pos, pnl, before, after)

        self._settled_count += 1
        if (self._cfg.report_every
                and self._settled_count % self._cfg.report_every == 0):
            for line in self._journal.calibration_report(
                    self._cfg.profile_name).split("\n"):
                LOG.info("| %s", line)

    def _reconcile(self, pos: Position, expected_pnl: float,
                   before: float, after: float) -> None:
        """
        Compare the actual balance change against the P&L we computed.

        The bot's own arithmetic and the venue's accounting should agree. When
        they do not, the venue is right and something here is wrong -- a fee we
        did not model, a partial fill, or a figure counted twice. Reporting the
        gap turns a silent drift into a visible one.

        A winning position is normally still unredeemed at this moment, so the
        balance may legitimately not have moved yet; that case is noted rather
        than flagged.
        """
        actual = after - before
        reference = max(abs(expected_pnl), self._cfg.min_stake_usdt)
        drift = abs(actual - expected_pnl)

        if self._unredeemed and abs(actual) < EPS:
            LOG.debug("Balance unchanged; winnings still unredeemed")
            return
        if drift <= reference * self._cfg.reconcile_tolerance:
            LOG.debug("Reconciled: expected %+.4f, actual %+.4f",
                      expected_pnl, actual)
            return

        LOG.warning(
            "RECONCILE MISMATCH on %s: expected %+.4f, balance moved %+.4f "
            "(gap %.4f). The venue is authoritative -- if the gap is close to "
            "the gross payout (%.4f) rather than the profit, something is "
            "counting the stake twice.",
            pos.rnd.slug, expected_pnl, actual, drift,
            pos.committed_usdt / max(pos.signal.fill_price, EPS))

    def _drain(self, timeout_s: Optional[float] = None) -> None:
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


def preflight(cfg: Config) -> int:
    """Probe every endpoint and report which ones actually work."""
    client = PredictionClient(cfg)
    print("\n=== Preflight ===\n")
    failures = 0

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
    check("funding plan", lambda: (
        lambda p: f"accountType={p[0]} fundingSource={p[1]} holder={p[2]}"
    )(client.funding_plan()))
    check("daily quota", lambda: f"{client.remaining_quota_usdt()}")

    rounds: list[Round] = []

    def list_rounds():
        rounds.extend(client.list_rounds())
        return f"{len(rounds)} live {cfg.symbol} round(s)"

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

    services = ("https://api.ipify.org?format=json",
                "https://ifconfig.me/all.json",
                "https://ipinfo.io/json")
    session = requests.Session()
    seen: collections.Counter = collections.Counter()
    errors: list[str] = []

    print(f"\nSampling the outbound IP {samples} times...\n")
    for i in range(samples):
        got = None
        for url in services:
            try:
                r = session.get(url, timeout=8)
                if r.status_code != 200:
                    continue
                body = r.json()
                got = body.get("ip") or body.get("ip_addr")
                if got:
                    break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{url}: {exc}")
        if got:
            seen[got] += 1
            print(f"  sample {i+1}: {got}")
        else:
            print(f"  sample {i+1}: could not determine")
        time.sleep(0.4)

    if not seen:
        print("\nNo IP could be determined. Outbound HTTP may be blocked.")
        for e in errors[:3]:
            print(f"  {e}")
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
            verdicts.append((amount, True, f"avg {q.average_price:.4f} "
                                           f"impact {q.price_impact:.4f}"))
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


def main(argv: Optional[Iterable[str]] = None) -> int:
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
                    help="comma-separated markets, e.g. BTCUSDT,ETHUSDT. "
                         "Each trades independently: its own position slot, "
                         "loss streak and calibration.")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="how many markets may hold a position at once")
    ap.add_argument("--report-symbol", default=None,
                    help="restrict a report to one market")
    ap.add_argument("--min-buffer", type=float, default=None,
                    help="override the buffer gate, in sigmas of the time "
                         "remaining")
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
    if args.scale_in is not None:
        overrides["scale_in"] = args.scale_in
    if args.report_every:
        overrides["report_every"] = args.report_every
    if args.no_fat_tails:
        overrides["use_fat_tails"] = False
    if args.symbols:
        overrides["symbols"] = tuple(
            x.strip().upper() for x in args.symbols.split(",") if x.strip())
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
