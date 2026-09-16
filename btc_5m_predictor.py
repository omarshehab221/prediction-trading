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

LAYOUT
------
This file is the entry point and the public surface: it defines nothing and
re-exports everything, so `python btc_5m_predictor.py` and
`import btc_5m_predictor as m` both still mean what they always did.

The bot is the btc5m package, one responsibility per module: constants, units,
errors and stats at the bottom; pricing, sizing and pnl above them; domain for
the value types; config, profiles and config_file for settings; volatility,
risk, journal, assessment and paper for the decision; venue/ for everything
said to the venue; trader/ for the loop and the four strategies; probes and cli
for what an operator runs. ws_feeds.py stays outside it: it is transport, and
it borrows only Side and two constants.
"""

from __future__ import annotations

import sys

# Run as a script, this file is the main module, not btc_5m_predictor.
# Anything that later imports btc_5m_predictor would then execute it a
# second time -- which, when the bot was defined here, meant a second Side
# class, `side is Side.UP` failing, and the UP ladder derived from the DOWN
# side of the book. Nothing is defined here any more, but registering the
# running module under its own name keeps there being only one of it.
if __name__ != "btc_5m_predictor":
    sys.modules.setdefault("btc_5m_predictor", sys.modules[__name__])

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
from btc5m.sizing import (
    boosted_stake,
    kelly_multiple,
    kelly_stake,
    max_topup_within_blend,
    straddle_completion_band,
    straddle_completion_stake,
    straddle_split,
    walk_book,
)
from btc5m.pnl import settle_pnl, straddle_worst_case_pnl, wins_per_loss
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
from btc5m.volatility import VolatilityEstimator, _projected_rounds
from btc5m.risk import RiskManager
from btc5m.journal import Journal
from btc5m.assessment import (
    Assessment,
    _DECLINE_ORDER,
    _worse,
    assess,
    blended_price_cap,
    clears_edge,
    clears_return,
    entry_window_start_s,
)
from btc5m.paper import PaperBook
from btc5m.probes import (
    IP_SERVICES,
    discover_min,
    outbound_ip,
    preflight,
    wait_for_auth,
    whoami,
)
from btc5m.cli import _parse_symbols_arg, main
from btc5m.venue.client import PredictionClient
from btc5m.trader.core import Trader


if __name__ == "__main__":
    raise SystemExit(main())
