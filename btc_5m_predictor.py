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
from btc5m.venue.spot import SpotApiMixin
from btc5m.venue.account import AccountApiMixin
from btc5m.venue.markets import MarketsApiMixin
from btc5m.venue.orders import OrdersApiMixin
from btc5m.venue.settlement import SettlementApiMixin
from btc5m.venue.client import PredictionClient
from btc5m.trader.accounting import AccountingMixin
from btc5m.trader.claims import ClaimsMixin
from btc5m.trader.bookkeeping import BookkeepingMixin
from btc5m.trader.order_lifecycle import OrderLifecycleMixin
from btc5m.trader.exits import ExitsMixin
from btc5m.trader.settling import SettlementMixin
from btc5m.trader.scale_in import ScaleInMixin
from btc5m.trader.straddle import StraddleMixin
from btc5m.trader.model_entry import ModelEntryMixin
from btc5m.trader.last_minute import LastMinuteMixin
from btc5m.trader.scalp import ScalpMixin
from btc5m.trader.core import Trader

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


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Strategy (pure)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


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
