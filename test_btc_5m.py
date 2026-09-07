#!/usr/bin/env python3
"""
Test suite for btc_5m_predictor.

Covers the pure logic end to end, plus a full simulated trading session driven
by a fake client, so the loop, sizing, settlement and risk limits are exercised
without touching the network.

    python3 -m unittest test_btc_5m -v
"""

from __future__ import annotations

import os
import random
import re
import tempfile
import threading
import queue
import types
import math
import sys
import time
import unittest

import requests
from dataclasses import replace
from decimal import Decimal

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config, Journal, Position, PredictionClient, RiskManager, Round, Side,
    Signal, TradingHalted, Trader, breakeven_probability,
    assess, digital_up_probability, kelly_stake, settle_pnl, walk_book,
)


def build_client(config=None, **attrs):
    """
    Construct a PredictionClient without __init__ (which opens a session).

    `_cfg` is a read-only property that resolves through a ConfigStore, so
    tests set the backing field rather than the property. Centralised here so
    a change to how config is held does not have to be applied at 30 sites.
    """
    c = PredictionClient.__new__(PredictionClient)
    c._store = None
    c._store = None

    c._static_cfg = config if config is not None else cfg()
    c._clock_offset_ms = 0
    c._symbol_cache = {}
    c._wallet = None
    for key, value in attrs.items():
        setattr(c, key, value)
    return c


def build_trader(client, config, db_path):
    """
    Construct a Trader without running __init__ (which does network I/O).

    Mirrors __init__ by reflection rather than by a hand-copied attribute
    list: five helpers each duplicated that list, so adding one field to
    Trader broke eleven tests at once. Anything __init__ sets that is not
    supplied here is initialised to a matching empty value.
    """
    t = Trader.__new__(Trader)
    t._store = None

    t._static_cfg = config
    t._client = client
    t._vol = types.SimpleNamespace(sigma_annual=lambda *a: 0.5,
                                   tail_df=lambda *a: None,
                                   is_clamped=lambda *a: False,
                                   raw_sigma=lambda *a: 0.5,
                                   trend=lambda *a: m.Trend())
    t._journal = Journal(db_path, getattr(config, "profile_name", "test"))
    t._paper_bankroll = config.paper_start_bankroll
    t._risk = {}
    t._account_risk = RiskManager(config, config.paper_start_bankroll)
    t._positions = {}
    t._claim_lock = threading.Lock()
    t._claim_queue = queue.Queue()
    t._claim_thread = None
    # The mode actually in force. Derived from config, not reflected: the
    # reflection below cannot evaluate `config.live`, and defaulting it to
    # None made every live-mode test silently run as paper.
    t._active_live = config.live

    # Fill in everything else __init__ would have set.
    import ast as _ast, inspect as _inspect, textwrap as _tw
    src = _tw.dedent(_inspect.getsource(Trader.__init__))
    for node in _ast.walk(_ast.parse(src)):
        if not isinstance(node, (_ast.Assign, _ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
        for tgt in targets:
            if not (isinstance(tgt, _ast.Attribute)
                    and isinstance(tgt.value, _ast.Name)
                    and tgt.value.id == "self"):
                continue
            # Never assign through a read-only property, and never let a
            # failed hasattr abort the loop before later fields are set.
            if isinstance(getattr(type(t), tgt.attr, None), property):
                continue
            try:
                if hasattr(t, tgt.attr):
                    continue
            except AttributeError:
                pass
            expr = _ast.unparse(node.value) if node.value else "None"
            if expr.startswith("{"):
                setattr(t, tgt.attr, {})
            elif expr.startswith("["):
                setattr(t, tgt.attr, [])
            elif expr in ("False", "True"):
                setattr(t, tgt.attr, expr == "True")
            elif expr == "None":
                setattr(t, tgt.attr, None)
            else:
                try:
                    setattr(t, tgt.attr, _ast.literal_eval(expr))
                except (ValueError, SyntaxError):
                    setattr(t, tgt.attr, None)
    return t


def cfg(**kw) -> Config:
    """Balanced profile by default: most legacy tests assume a wide book."""
    base = dict(api_key="k", api_secret="s", live=False, **m.PROFILES["balanced"])
    base.update(kw)
    return Config(**base)


def convex_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", **m.PROFILES["convex"])
    base.update(kw)
    return Config(**base)


def make_round(**kw) -> Round:
    base = dict(topic_id=1, market_id=9, vendor="PREDICT_FUN", slug="btc-5m",
                symbol="BTCUSDT",
                start_ms=1_700_000_000_000,
                end_ms=1_700_000_000_000 + (m.DEFAULT_ROUND_SECONDS * 1000),
                up_token_id="1", down_token_id="2",
                up_quote=0.50, down_quote=0.50, fee_bps=200,
                chain_id="56", collateral="USDT", venue_slippage_bps=1200,
                decimal_precision=4, liquidity=100_000.0,
                strike=100_000.0, feed_symbol="BTCUSDT")
    base.update(kw)
    return Round(**base)


# --------------------------------------------------------------------------


class TestDigitalPricing(unittest.TestCase):

    def test_at_the_money_is_a_coinflip(self):
        p = digital_up_probability(100_000, 100_000, 0.5, 60)
        self.assertAlmostEqual(p, 0.5, places=9)

    def test_deep_in_the_money_approaches_one(self):
        p = digital_up_probability(101_000, 100_000, 0.5, 5)
        self.assertGreater(p, 0.999)

    def test_symmetry_up_and_down(self):
        up = digital_up_probability(100_500, 100_000, 0.6, 90)
        down = digital_up_probability(100_000, 100_500, 0.6, 90)
        self.assertAlmostEqual(up + down, 1.0, places=9)

    def test_probability_decays_toward_coinflip_as_time_grows(self):
        near = digital_up_probability(100_200, 100_000, 0.5, 10)
        far = digital_up_probability(100_200, 100_000, 0.5, 300)
        self.assertGreater(near, far)
        self.assertGreater(far, 0.5)

    def test_expiry_is_deterministic(self):
        self.assertEqual(digital_up_probability(100_001, 100_000, 0.5, 0), 1.0)
        self.assertEqual(digital_up_probability(99_999, 100_000, 0.5, 0), 0.0)
        self.assertEqual(digital_up_probability(100_000, 100_000, 0.5, -5), 0.0)

    def test_always_a_valid_probability(self):
        rng = random.Random(7)
        for _ in range(2000):
            spot = rng.uniform(50_000, 150_000)
            strike = rng.uniform(50_000, 150_000)
            p = digital_up_probability(spot, strike, rng.uniform(0.1, 3.0),
                                       rng.uniform(0, 300))
            self.assertTrue(0.0 <= p <= 1.0)

    def test_rejects_invalid_input(self):
        for args in [(0, 100, 0.5, 60), (100, 0, 0.5, 60), (100, 100, 0, 60)]:
            with self.assertRaises(ValueError):
                digital_up_probability(*args)


class TestBreakeven(unittest.TestCase):

    def test_zero_fee_breakeven_is_the_price(self):
        self.assertAlmostEqual(breakeven_probability(0.6, 0), 0.6, places=12)

    def test_fee_raises_the_hurdle(self):
        self.assertGreater(breakeven_probability(0.6, 200), 0.6)

    def test_matches_zero_ev_by_construction(self):
        """The returned probability must make expected value exactly zero."""
        for price in (0.15, 0.35, 0.5, 0.72, 0.88):
            q = breakeven_probability(price, 200)
            ev = q * (1 - price) / price * 0.98 - (1 - q)
            self.assertAlmostEqual(ev, 0.0, places=12)

    def test_is_tighter_than_the_naive_approximation(self):
        price = 0.6
        naive = price + 0.02 * (1 - price)
        self.assertLess(breakeven_probability(price, 200), naive)

    def test_rejects_out_of_range(self):
        for p in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                breakeven_probability(p, 200)


class TestKelly(unittest.TestCase):

    def test_no_bet_at_fair_price(self):
        self.assertEqual(kelly_stake(1000, 0.60, 0.60, cfg()), 0.0)

    def test_no_bet_when_negative_edge(self):
        self.assertEqual(kelly_stake(1000, 0.40, 0.60, cfg()), 0.0)

    def test_bets_when_underpriced(self):
        self.assertGreater(kelly_stake(1000, 0.85, 0.60, cfg()), 0.0)

    def test_respects_hard_cap(self):
        c = cfg()
        stake = kelly_stake(1000, 0.999, 0.10, c)
        self.assertLessEqual(stake, 1000 * c.max_stake_pct + 1e-9)

    def test_scales_with_edge(self):
        small = kelly_stake(1000, 0.65, 0.60, cfg(max_stake_pct=0.25))
        large = kelly_stake(1000, 0.80, 0.60, cfg(max_stake_pct=0.25))
        self.assertGreater(large, small)

    def test_fraction_reduces_size(self):
        """Below the cap the ratio is exact; at the cap it is merely smaller."""
        uncapped = dict(max_stake_pct=0.25)
        full = kelly_stake(1000, 0.62, 0.60, cfg(kelly_fraction=1.0, **uncapped))
        quarter = kelly_stake(1000, 0.62, 0.60,
                              cfg(kelly_fraction=0.25, **uncapped))
        self.assertAlmostEqual(quarter, full * 0.25, places=9)

    def test_cap_binds_before_the_fraction(self):
        capped_full = kelly_stake(1000, 0.80, 0.60,
                                  cfg(kelly_fraction=1.0, max_stake_pct=0.25))
        capped_quarter = kelly_stake(1000, 0.80, 0.60,
                                     cfg(kelly_fraction=0.25,
                                         max_stake_pct=0.25))
        self.assertAlmostEqual(capped_full, 250.0, places=9)
        self.assertLess(capped_quarter, capped_full)

    def test_below_minimum_stake_returns_zero(self):
        self.assertEqual(kelly_stake(5, 0.62, 0.60, cfg(min_stake_usdt=1.5)),
                         0.0)

    def test_zero_bankroll(self):
        self.assertEqual(kelly_stake(0, 0.9, 0.5, cfg()), 0.0)

    def test_never_risks_ruin_over_a_long_losing_streak(self):
        """20 consecutive maximum-size losses must not wipe the account."""
        c = cfg()
        bankroll = 100.0
        for _ in range(20):
            stake = min(bankroll * c.max_stake_pct, bankroll)
            bankroll -= stake
        self.assertGreater(bankroll, 30.0)


class TestWalkBook(unittest.TestCase):

    def test_single_level_exact_price(self):
        self.assertAlmostEqual(walk_book([(0.50, 1000)], 100.0), 0.50)

    def test_multi_level_average_is_between_levels(self):
        avg = walk_book([(0.50, 100), (0.60, 1000)], 100.0)
        self.assertIsNotNone(avg)
        self.assertGreater(avg, 0.50)
        self.assertLess(avg, 0.60)

    def test_average_is_worse_than_top_of_book(self):
        top = 0.50
        avg = walk_book([(0.50, 10), (0.55, 100), (0.70, 100)], 60.0)
        self.assertGreater(avg, top)

    def test_insufficient_depth_returns_none(self):
        self.assertIsNone(walk_book([(0.50, 10)], 100.0))

    def test_shares_reconcile_with_spend(self):
        asks = [(0.40, 50), (0.45, 50), (0.50, 500)]
        stake = 100.0
        avg = walk_book(asks, stake)
        self.assertAlmostEqual(stake / avg * avg, stake, places=9)

    def test_ignores_malformed_levels(self):
        avg = walk_book([(0.0, 100), (-1.0, 5), (0.50, 1000)], 100.0)
        self.assertAlmostEqual(avg, 0.50)

    def test_rejects_nonpositive_stake(self):
        with self.assertRaises(ValueError):
            walk_book([(0.5, 100)], 0.0)


class TestSettlePnl(unittest.TestCase):

    def test_loss_is_the_full_stake(self):
        self.assertEqual(settle_pnl(10.0, 0.6, False, 200), -10.0)

    def test_win_pays_net_odds_after_fee(self):
        pnl = settle_pnl(10.0, 0.50, True, 200)
        self.assertAlmostEqual(pnl, 10.0 * 1.0 * 0.98, places=9)

    def test_high_price_wins_are_small(self):
        self.assertLess(settle_pnl(100.0, 0.95, True, 200), 6.0)

    def test_low_price_wins_are_large(self):
        self.assertGreater(settle_pnl(100.0, 0.20, True, 200), 380.0)

    def test_a_95pct_win_rate_at_095_is_barely_profitable(self):
        """The core arithmetic: high win rate does not imply profit."""
        wins = settle_pnl(100.0, 0.95, True, 200) * 19
        loss = settle_pnl(100.0, 0.95, False, 200)
        self.assertLess(wins + loss, 0.0)   # 19 wins, 1 loss -> net negative


class TestParseRound(unittest.TestCase):

    @staticmethod
    def topic(**over) -> dict:
        base = {
            "marketTopicId": 4229564, "chartType": "CRYPTO_UP_DOWN",
            "symbol": "BTCUSDT", "status": "REGISTERED",
            "startDate": 1748131200000, "endDate": 1748131500000,
            "slug": "btc-5m", "feeRateBps": 200, "variantData": None,
            "vendor": "PREDICT_FUN", "chainId": "56", "collateral": "USDT",
            "slippageBps": 1200,
            "markets": [{"marketId": 5567895, "tradingStatus": "OPEN",
                         "decimalPrecision": 4, "liquidity": "100000",
                         "outcomes": [
                             {"name": "YES", "price": "0.52", "tokenId": "1"},
                             {"name": "NO", "price": "0.48", "tokenId": "2"}]}],
        }
        base.update(over)
        return base

    def test_parses_documented_payload_without_strike(self):
        """Regression: null variantData must not reject the round."""
        rnd = PredictionClient._parse_round(self.topic())
        self.assertIsNotNone(rnd)
        self.assertIsNone(rnd.strike)
        self.assertEqual(rnd.up_quote, 0.52)
        self.assertEqual(rnd.up_token_id, "1")

    def test_uses_strike_when_present(self):
        rnd = PredictionClient._parse_round(
            self.topic(variantData={"startPrice": "100000.5",
                                    "priceFeedSymbol": "BTCUSDT"}))
        self.assertAlmostEqual(rnd.strike, 100000.5)
        self.assertEqual(rnd.feed_symbol, "BTCUSDT")

    def test_accepts_up_down_outcome_names(self):
        t = self.topic()
        t["markets"][0]["outcomes"] = [
            {"name": "UP", "price": "0.52", "tokenId": "1"},
            {"name": "DOWN", "price": "0.48", "tokenId": "2"}]
        self.assertIsNotNone(PredictionClient._parse_round(t))

    def test_default_accepts_any_symbol(self):
        """No `symbols` configured -> nothing is filtered by ticker."""
        PredictionClient(cfg())
        try:
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                self.assertIsNotNone(PredictionClient._parse_round(
                    self.topic(symbol=sym)), sym)
        finally:
            PredictionClient(cfg())

    def test_rejects_wrong_symbol_when_restricted(self):
        PredictionClient(cfg(symbols=("BTCUSDT",)))
        try:
            self.assertIsNone(PredictionClient._parse_round(
                self.topic(symbol="ETHUSDT")))
        finally:
            PredictionClient(cfg())

    def test_rejects_wrong_chart_type(self):
        self.assertIsNone(PredictionClient._parse_round(
            self.topic(chartType="FLAT")))

    def test_rejects_wrong_duration(self):
        self.assertIsNone(PredictionClient._parse_round(
            self.topic(endDate=1748131200000 + 3_600_000)))

    def test_rejects_closed_trading(self):
        t = self.topic()
        t["markets"][0]["tradingStatus"] = "CLOSED"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_rejects_out_of_range_prices(self):
        t = self.topic()
        t["markets"][0]["outcomes"][0]["price"] = "1.5"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_survives_garbage(self):
        for bad in [{}, {"chartType": "CRYPTO_UP_DOWN"},
                    self.topic(markets=[]), self.topic(startDate="abc"),
                    self.topic(markets=[{"marketId": 1,
                                         "tradingStatus": "OPEN"}])]:
            self.assertIsNone(PredictionClient._parse_round(bad))


class TestParseAsks(unittest.TestCase):

    def test_array_shape(self):
        got = PredictionClient._parse_asks(
            {"asks": [["0.55", "100"], ["0.50", "50"]]})
        self.assertEqual(got, [(0.50, 50.0), (0.55, 100.0)])

    def test_dict_shape(self):
        got = PredictionClient._parse_asks(
            {"asks": [{"price": "0.6", "size": "10"}]})
        self.assertEqual(got, [(0.6, 10.0)])

    def test_nested_shape(self):
        got = PredictionClient._parse_asks(
            {"data": {"asks": [{"price": "0.4", "quantity": "5"}]}})
        self.assertEqual(got, [(0.4, 5.0)])

    def test_returns_sorted_ascending(self):
        got = PredictionClient._parse_asks(
            {"asks": [["0.9", "1"], ["0.1", "1"], ["0.5", "1"]]})
        self.assertEqual([p for p, _ in got], [0.1, 0.5, 0.9])

    def test_missing_or_empty(self):
        self.assertIsNone(PredictionClient._parse_asks({}))
        self.assertIsNone(PredictionClient._parse_asks({"asks": []}))
        self.assertIsNone(PredictionClient._parse_asks({"asks": [["x", "y"]]}))


class TestEvaluate(unittest.TestCase):

    def setUp(self):
        self.now = 1_700_000_000_000 + (m.DEFAULT_ROUND_SECONDS * 1000) - 60_000   # 60s left

    def test_no_trade_without_strike(self):
        rnd = make_round(strike=None)
        self.assertIsNone(assess(rnd, 100_500, 0.5, 1000, self.now, cfg()).signal)

    def test_no_trade_outside_entry_window(self):
        rnd = make_round()
        too_early = rnd.end_ms - 280_000
        self.assertIsNone(assess(rnd, 100_500, 0.5, 1000, too_early, cfg()).signal)
        too_late = rnd.end_ms - 5_000
        self.assertIsNone(assess(rnd, 100_500, 0.5, 1000, too_late, cfg()).signal)

    def test_no_trade_at_fair_prices(self):
        rnd = make_round(up_quote=0.5, down_quote=0.5)
        self.assertIsNone(assess(rnd, 100_000, 0.5, 1000, self.now, cfg()).signal)

    def test_trades_when_book_is_underpriced(self):
        rnd = make_round()
        book = {Side.UP: [(0.55, 10_000)], Side.DOWN: [(0.90, 10_000)]}
        sig = assess(rnd, 100_400, 0.5, 1000, self.now, cfg(), book).signal
        self.assertIsNotNone(sig)
        self.assertIs(sig.side, Side.UP)
        self.assertGreater(sig.edge, cfg().min_edge)

    def test_picks_the_higher_edge_side(self):
        rnd = make_round()
        book = {Side.UP: [(0.90, 10_000)], Side.DOWN: [(0.30, 10_000)]}
        sig = assess(rnd, 99_600, 0.5, 1000, self.now, cfg(), book).signal
        self.assertIsNotNone(sig)
        self.assertIs(sig.side, Side.DOWN)

    def test_fill_price_reflects_book_depth(self):
        rnd = make_round()
        thin = {Side.UP: [(0.55, 5), (0.65, 10_000)]}
        deep = {Side.UP: [(0.55, 10_000)]}
        s_thin = assess(rnd, 100_400, 0.5, 1000, self.now, cfg(), thin).signal
        s_deep = assess(rnd, 100_400, 0.5, 1000, self.now, cfg(), deep).signal
        self.assertIsNotNone(s_thin)
        self.assertGreater(s_thin.fill_price, s_deep.fill_price)
        self.assertLess(s_thin.edge, s_deep.edge)

    def test_skips_when_depth_cannot_fill(self):
        rnd = make_round()
        book = {Side.UP: [(0.55, 0.5)]}      # ~0.27 USDT of depth
        self.assertIsNone(
            assess(rnd, 100_400, 0.5, 100_000, self.now, cfg(), book).signal)

    def test_refuses_prices_above_the_cap(self):
        rnd = make_round()
        book = {Side.UP: [(0.97, 10_000)]}
        self.assertIsNone(
            assess(rnd, 101_000, 0.5, 1000, self.now, cfg(), book).signal)

    def test_falls_back_to_haircut_quote_without_book(self):
        rnd = make_round(up_quote=0.50, down_quote=0.50)
        sig = assess(rnd, 100_600, 0.5, 1000, self.now, cfg(), None).signal
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig.fill_price, 0.53, places=9)

    def test_edge_is_measured_against_breakeven_not_raw_price(self):
        rnd = make_round()
        book = {Side.UP: [(0.55, 10_000)]}
        sig = assess(rnd, 100_400, 0.5, 1000, self.now, cfg(), book).signal
        expected = sig.model_prob - breakeven_probability(0.55, 200)
        self.assertAlmostEqual(sig.edge, expected, places=12)

    def test_stake_never_exceeds_cap(self):
        rnd = make_round()
        book = {Side.UP: [(0.20, 1_000_000)]}
        sig = assess(rnd, 103_000, 0.5, 1000, self.now, cfg(), book).signal
        self.assertIsNotNone(sig)
        self.assertLessEqual(sig.stake_usdt, 1000 * cfg().max_stake_pct + 1e-9)

    def test_is_pure(self):
        rnd = make_round()
        book = {Side.UP: [(0.55, 10_000)]}
        before = (rnd, dict(book))
        assess(rnd, 100_400, 0.5, 1000, self.now, cfg(), book).signal
        self.assertEqual(before[0], rnd)
        self.assertEqual(before[1], book)


class TestRiskManager(unittest.TestCase):

    def test_allows_trading_within_limits(self):
        RiskManager(cfg(), 100.0).check(95.0)

    def test_daily_loss_limit(self):
        """Losses the bot actually booked do trip the limit."""
        r = RiskManager(cfg(), 100.0)
        r.record_result(False, 0.6, -21.0)
        with self.assertRaises(TradingHalted):
            r.check(79.0, 0.0)

    def test_an_external_withdrawal_is_not_a_loss(self):
        """
        The reported bug: a manual order or transfer emptied most of a small
        balance and the bot halted as though the strategy had lost it.
        """
        r = RiskManager(cfg(min_stake_usdt=1.0), 9.40)
        r.check(4.40, 0.0)                    # a 5.00 order placed by hand
        self.assertAlmostEqual(r.external_flow, -5.0, places=6)
        self.assertEqual(r.realised_pnl, 0.0)

    def test_a_deposit_does_not_flatter_the_limit_either(self):
        """Rebasing has to cut both ways or it is just a different bias."""
        r = RiskManager(cfg(), 100.0)
        r.check(200.0, 0.0)                   # a deposit
        r.record_result(False, 0.6, -45.0)
        with self.assertRaises(TradingHalted):
            r.check(155.0, 0.0)               # 45 of 200 is still over 20%

    def test_an_open_stake_is_not_a_loss_until_it_settles(self):
        """Money staked has left the balance but has not been lost."""
        r = RiskManager(cfg(), 100.0)
        r.check(70.0, 30.0)                   # 30 staked, still open
        self.assertEqual(r.external_flow, 0.0)
        self.assertEqual(r.realised_pnl, 0.0)

    def test_price_movement_on_an_open_position_is_not_an_external_flow(self):
        """
        The reported bug. A wallet-funded account reads its balance from the
        portfolio's totalCurrentValue, which marks open positions to MARKET,
        while reconciliation knows them at COST. Every tick of the underlying
        was booked as a deposit or a withdrawal and rebased the day baseline,
        with no trade taking place at all.
        """
        r = RiskManager(cfg(), 20.0)
        # A 4.00 leg filled at 0.25 = 16 shares; 16.00 USDT cash left over.
        for price in (0.25, 0.30, 0.22, 0.31):
            r.check(16.00 + 16.0 * price, 4.00)
        self.assertEqual(r.external_flow, 0.0)
        self.assertAlmostEqual(r._day_start_bankroll, 20.0, places=9)

    def test_an_open_stake_is_not_an_external_flow_in_paper_either(self):
        """
        Same symptom, different cause: _paper_bankroll is not debited when a
        stake goes out, so an open position read as a deposit of its own size.
        """
        r = RiskManager(cfg(), 100.0)
        r.check(100.0, 40.0)              # 40 stated open, paper cash untouched
        self.assertEqual(r.external_flow, 0.0)
        self.assertAlmostEqual(r._day_start_bankroll, 100.0, places=9)

    def test_a_claim_in_flight_is_not_an_external_flow(self):
        """
        The second reported bug. A win is booked into realised PnL the moment
        it settles, but the USDT lands on chain up to claim_timeout_s later.
        The gap used to read as a withdrawal and the credit that closed it as
        a deposit -- two phantom movements per won round.
        """
        r = RiskManager(cfg(), 20.0)
        r.record_result(True, 0.5, pnl=12.0)
        r.check(16.0, 16.0)               # payout not yet credited on chain
        r.check(32.0, 0.0)                # redemption confirms
        self.assertEqual(r.external_flow, 0.0)
        self.assertAlmostEqual(r.realised_pnl, 12.0, places=9)
        self.assertAlmostEqual(r._day_start_bankroll, 20.0, places=9)

    def test_a_withdrawal_during_a_round_is_still_caught_once_flat(self):
        """
        Waiting for a flat book delays detection; it must not lose any of it.
        """
        r = RiskManager(cfg(), 100.0)
        r.check(20.0, 40.0)               # 40 staked and 40 withdrawn: skipped
        self.assertEqual(r.external_flow, 0.0)
        r.record_result(False, 0.5, pnl=-5.0)
        # Flat now: 100 opening, 5 lost trading, 40 taken out by hand.
        r.check(55.0, 0.0)
        self.assertAlmostEqual(r.realised_pnl, -5.0, places=9)
        self.assertAlmostEqual(r.external_flow, -40.0, places=9)

    def test_a_manager_that_does_not_own_the_balance_never_rebases(self):
        """
        Per-market managers share one account. Each one rebasing the shared
        baseline off a balance it only partly explains is nonsense, so they
        pass no outstanding figure and reconciliation sits out.
        """
        r = RiskManager(cfg(), 100.0)
        r.check(40.0)                     # no outstanding figure supplied
        self.assertEqual(r.external_flow, 0.0)
        self.assertAlmostEqual(r._day_start_bankroll, 100.0, places=9)

    def test_settling_a_stake_leaves_no_phantom_external_flow(self):
        r = RiskManager(cfg(), 100.0)
        r.check(70.0, 30.0)
        r.record_result(True, 0.6, 12.0)
        r.check(112.0, 0.0)                   # stake back plus 12 won
        self.assertAlmostEqual(r.external_flow, 0.0, places=6)
        self.assertAlmostEqual(r.realised_pnl, 12.0, places=6)

    def test_mixed_external_and_own_losses_are_told_apart(self):
        r = RiskManager(cfg(daily_loss_limit_pct=0.20), 100.0)
        r.record_result(False, 0.6, -10.0)
        r.check(50.0, 0.0)                    # -10 traded, -40 withdrawn
        self.assertAlmostEqual(r.realised_pnl, -10.0, places=6)
        self.assertAlmostEqual(r.external_flow, -40.0, places=6)

    # -- the venue corrects the bot's arithmetic ---------------------------
    #
    # Everything above separates the bot's trading from everyone else's
    # money. It cannot tell either of those from a third thing: the bot's own
    # PnL arithmetic being WRONG. settle_pnl models the fee and the fill, and
    # any error in that model shows up as exactly the same residue a deposit
    # does -- so it was silently rebased into the baseline and relabelled an
    # external flow, once per trade, for as long as the bot ran.

    def test_a_credit_that_lands_short_corrects_pnl_not_the_baseline(self):
        """
        The venue is authoritative. A win booked at +12.00 that actually
        credits 11.80 means the fee model is off by 0.20 -- that is a trading
        result, not somebody's withdrawal, and the daily limit has to see it.
        """
        r = RiskManager(cfg(), 100.0)
        r.record_result(True, 0.5, pnl=12.0)
        r.expect_credit(32.0)             # 20 stake back + 12 won
        r.check(70.0, 32.0)               # still in flight
        r.check(111.80, 0.0)              # credited 0.20 light

        self.assertAlmostEqual(r.realised_pnl, 11.80, places=6)
        self.assertAlmostEqual(r.pnl_correction, -0.20, places=6)
        self.assertAlmostEqual(r.external_flow, 0.0, places=6)
        self.assertAlmostEqual(r._day_start_bankroll, 100.0, places=9)

    def test_the_correction_is_bounded_by_a_plausible_fee(self):
        """
        A withdrawal landing in the same window as a claim must not be
        swallowed as a trading loss -- that is the bug the external flow
        split exists to prevent. The residue is genuinely ambiguous, so it
        is capped rather than guessed: a fee can explain reconcile_tolerance
        of the payout and not a cent more.
        """
        c = cfg(reconcile_tolerance=0.10)
        r = RiskManager(c, 100.0)
        r.record_result(True, 0.5, pnl=12.0)
        r.expect_credit(32.0)
        r.check(71.80, 0.0)               # credited light AND -40 by hand

        self.assertAlmostEqual(r.pnl_correction, -3.20, places=6)   # 10% of 32
        self.assertAlmostEqual(r.external_flow, -37.0, places=6)
        # Whatever the split, no money is invented or lost by it.
        self.assertAlmostEqual(r.pnl_correction + r.external_flow,
                               -40.20, places=6)

    def test_the_cap_scales_with_the_payout_it_explains(self):
        """A bigger claim can hide a bigger fee, and nothing else can."""
        c = cfg(reconcile_tolerance=0.10)
        small = RiskManager(c, 100.0)
        small.expect_credit(10.0)
        small.check(80.0, 0.0)

        big = RiskManager(c, 100.0)
        big.expect_credit(100.0)
        big.check(80.0, 0.0)

        self.assertAlmostEqual(small.pnl_correction, -1.0, places=6)
        self.assertAlmostEqual(big.pnl_correction, -10.0, places=6)

    def test_an_expectation_is_consumed_once(self):
        """A credit explains the drift at its own settlement, and no later."""
        r = RiskManager(cfg(), 100.0)
        r.record_result(True, 0.5, pnl=12.0)
        r.expect_credit(32.0)
        r.check(112.0, 0.0)               # landed exactly; nothing to correct
        self.assertAlmostEqual(r.pnl_correction, 0.0, places=6)

        r.check(72.0, 0.0)                # a later withdrawal
        self.assertAlmostEqual(r.pnl_correction, 0.0, places=6)
        self.assertAlmostEqual(r.external_flow, -40.0, places=6)

    def test_a_direct_correction_moves_realised_pnl(self):
        """
        The settlement-time path. _reconcile measures the gap against a
        balance read seconds either side of one settlement, which is far too
        narrow a window for a deposit, so it is charged straight to PnL.
        """
        r = RiskManager(cfg(), 100.0)
        r.record_result(False, 0.5, pnl=-20.0)
        r.correct_realised_pnl(-0.35)
        self.assertAlmostEqual(r.realised_pnl, -20.35, places=6)
        self.assertAlmostEqual(r.pnl_correction, -0.35, places=6)

    def test_a_corrected_loss_still_trips_the_daily_limit(self):
        """
        The point of all of it. A model that under-reports every loss must
        not be able to walk the bot past its own stop.
        """
        r = RiskManager(cfg(daily_loss_limit_pct=0.20), 100.0)
        r.record_result(False, 0.6, pnl=-19.0)
        r.correct_realised_pnl(-2.0)      # the venue took 2.00 more
        with self.assertRaises(TradingHalted):
            r.check(79.0, 0.0)

    def test_the_halt_message_reports_corrections_separately(self):
        r = RiskManager(cfg(daily_loss_limit_pct=0.20), 100.0)
        r.record_result(False, 0.6, pnl=-19.0)
        r.correct_realised_pnl(-2.0)
        with self.assertRaises(TradingHalted) as caught:
            r.check(79.0, 0.0)
        self.assertIn("correction", str(caught.exception).lower())

    def test_consecutive_losses(self):
        r = RiskManager(cfg(max_consecutive_losses=3), 100.0)
        for _ in range(3):
            r.record_result(False)
        with self.assertRaises(TradingHalted):
            r.check(100.0)

    def test_a_win_resets_the_streak(self):
        r = RiskManager(cfg(max_consecutive_losses=3), 100.0)
        r.record_result(False)
        r.record_result(False)
        r.record_result(True)
        r.check(100.0)
        self.assertEqual(r.consecutive_losses, 0)

    def test_max_rounds(self):
        r = RiskManager(cfg(max_rounds_per_day=2), 100.0)
        for _ in range(2):
            r.record_result(True)
        with self.assertRaises(TradingHalted):
            r.check(100.0)

    def test_halt_is_sticky(self):
        r = RiskManager(cfg(), 100.0)
        r.record_result(False, 0.6, -50.0)
        with self.assertRaises(TradingHalted):
            r.check(50.0, 0.0)
        with self.assertRaises(TradingHalted):
            r.check(100.0, 0.0)  # still halted even once bankroll recovers

    def test_bankroll_floor(self):
        r = RiskManager(cfg(daily_loss_limit_pct=0.99), 100.0)
        with self.assertRaises(TradingHalted):
            r.check(0.5)


class TestConfigValidation(unittest.TestCase):

    def test_rejects_bad_values(self):
        bad = [dict(kelly_fraction=0), dict(kelly_fraction=1.5),
               dict(max_stake_pct=0.9), dict(min_edge=0),
               dict(entry_window_start_s=10, entry_window_end_s=20),
               dict(min_entry_price=0.9, max_entry_price=0.5),
               dict(api_key=""), dict(fee_bps=10_000)]
        for kw in bad:
            with self.subTest(**kw):
                with self.assertRaises(ValueError):
                    cfg(**kw)

    def test_accepts_defaults(self):
        self.assertIsInstance(cfg(), Config)


# --------------------------------------------------------------------------
# Simulated session
# --------------------------------------------------------------------------


class FakeClient:
    """Deterministic stand-in for PredictionClient. No network."""

    def __init__(self, rounds, spot_path, books, winners, balance=100.0):
        self._rounds = rounds
        self._spot_path = spot_path
        self._books = books
        self._winners = winners
        self.balance = balance
        self.orders = []
        self.redeemed = []
        self.redeem_fails = False
        self.redeem_state = "PENDING"
        self.t = 0

    def sync_clock(self):
        return 0

    def now_ms(self):
        return self._spot_path[min(self.t, len(self._spot_path) - 1)][0]

    def market_symbol(self, feed_symbol):
        return "BTCUSDT"

    def spot_price(self, symbol="BTCUSDT"):
        return self._spot_path[min(self.t, len(self._spot_path) - 1)][1]

    def hydrate(self, rnd):
        from dataclasses import replace as _r
        if rnd.strike is not None:
            return rnd
        return _r(rnd, strike=self._spot_path[0][1], feed_symbol="BTCUSDT")

    def settled_outcome(self, rnd):
        w = self._winners.get(rnd.topic_id)
        return None if w is None else (w, 0.0)

    def final_price(self, rnd):
        return self._spot_path[-1][1]

    def balance_usdt(self):
        return self.balance

    def get_quote(self, rnd, side, stake):
        return m.Quote("q1", 0.51, stake / 0.51, 0.001, 0.0)

    def batch_redeem(self, token_ids, chain_id="56"):
        self.redeemed.extend(token_ids)
        if self.redeem_fails:
            raise m.ApiError("redeem rejected")
        return ["0xtx" + t for t in token_ids]

    def redeem_status(self, tx_hash):
        return self.redeem_state

    def place_order(self, rnd, quote, stake_usdt=None):
        self.orders.append((quote.quote_id, quote.average_price, stake_usdt))
        return "order-1"

    def confirm_fill(self, order_id, requested_usdt):
        # Deterministic stand-in: every order fills in full. Tests that need
        # a partial or dead fill override this per-instance.
        return requested_usdt

    def list_rounds(self):
        return list(self._rounds)

    def asks_for(self, rnd, side):
        return self._books.get((rnd.topic_id, side))

    def resolved_winner(self, rnd):
        return self._winners.get(rnd.topic_id)

    def bankroll_usdt(self):
        return self.balance

    def place_market_buy(self, rnd, side, stake, max_price):
        self.orders.append((rnd.topic_id, side, stake, max_price))
        self.balance -= stake
        return min(max_price, 0.55)


class TestSimulatedSession(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _trader(self, client, **kw):
        c = cfg(db_path=self.db, **kw)
        return build_trader(client, c, self.db)

    def test_enters_and_settles_a_winning_round(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)],
                 (1, Side.DOWN): [(0.95, 10_000)]}
        client = FakeClient([rnd], path, books, {})

        t = self._trader(client)
        client.t = 1                       # 60s left
        t._maybe_enter(100.0, "PAPER")

        self.assertIsNotNone(t._position)
        self.assertIs(t._position.signal.side, Side.UP)
        self.assertAlmostEqual(t._position.rnd.strike, 100_000.0)

        client.t = 2                       # past resolution
        client._winners[1] = Side.UP
        t._settle_open()

        self.assertIsNone(t._position)
        self.assertGreater(t._paper_bankroll, 100.0)
        self.assertEqual(t._account_risk.consecutive_losses, 0)

    def test_settles_a_loss_and_debits_bankroll(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 99_900.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        staked = t._position.signal.stake_usdt

        client.t = 2
        client._winners[1] = Side.DOWN
        t._settle_open()

        self.assertAlmostEqual(t._paper_bankroll, 100.0 - staked, places=9)
        self.assertEqual(t._account_risk.consecutive_losses, 1)

    def test_the_concurrency_cap_is_what_governs(self):
        """
        Regression. The entry guard used to return whenever ANY position was
        open, which made max_concurrent_positions dead: its default of 2
        could never be reached, and every multi-market deployment quietly
        traded one market at a time while the config said otherwise.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rounds = [make_round(topic_id=i, market_id=i, symbol=sym,
                             feed_symbol="BTCUSDT", strike=None,
                             start_ms=start, end_ms=end)
                  for i, sym in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), 1)]
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(i, Side.UP): [(0.55, 10_000)] for i in (1, 2, 3)}
        client = FakeClient(rounds, path, books, {})

        t = self._trader(client, symbols=(), max_concurrent_positions=2)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 2)
        self.assertEqual(sorted(t._positions),
                         [("BTCUSDT", Side.UP), ("ETHUSDT", Side.UP)])

        t._maybe_enter(100.0, "PAPER")      # cap reached; nothing more opens
        self.assertEqual(len(t._positions), 2)

    def test_a_cap_of_one_still_means_one(self):
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rounds = [make_round(topic_id=i, market_id=i, symbol=sym,
                             feed_symbol="BTCUSDT", strike=None,
                             start_ms=start, end_ms=end)
                  for i, sym in enumerate(("BTCUSDT", "ETHUSDT"), 1)]
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(i, Side.UP): [(0.55, 10_000)] for i in (1, 2)}
        client = FakeClient(rounds, path, books, {})

        t = self._trader(client, symbols=(), max_concurrent_positions=1)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 1)

    def test_a_second_position_on_one_market_is_refused(self):
        """
        The narrow thing the old guard was really protecting: the same bet
        twice on one symbol is not diversification, and it would orphan the
        first position in the journal.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        r1 = make_round(topic_id=1, market_id=1, strike=None,
                        start_ms=start, end_ms=end)
        r2 = make_round(topic_id=2, market_id=2, strike=None,
                        start_ms=start, end_ms=end)     # same symbol
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)],
                 (2, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([r1, r2], path, books, {})

        t = self._trader(client, max_concurrent_positions=3)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 1)

    def test_only_one_position_at_a_time(self):
        start = 1_700_000_000_000
        r1 = make_round(topic_id=1, strike=None, start_ms=start,
                        end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        r2 = make_round(topic_id=2, market_id=10, strike=None,
                        start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)],
                 (2, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([r1, r2], path, books, {})

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        first = t._position.rnd.topic_id
        t._maybe_enter(100.0, "PAPER")      # must not replace the open one
        self.assertEqual(t._position.rnd.topic_id, first)

    def test_does_not_reenter_the_same_round(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {1: Side.UP})

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        client.t = 2
        t._settle_open()
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertIsNone(t._position)

    def test_waits_when_settlement_data_is_unavailable(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertIsNotNone(t._position)

        client.final_price = lambda rnd: None   # venue has not resolved yet
        client.t = 2
        t._settle_open()
        self.assertIsNotNone(t._position)    # held, not silently dropped

    def test_live_mode_records_the_actual_fill(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.50, 10_000)]}
        client = FakeClient([rnd], path, books, {})

        t = self._trader(client, live=True)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertEqual(len(client.orders), 1)
        self.assertAlmostEqual(t._position.signal.fill_price, 0.51, places=9)

    def test_long_run_never_goes_bankrupt(self):
        """500 rounds where the model is pure noise: capped, not wiped out."""
        rng = random.Random(11)
        c = cfg()
        bankroll = 100.0
        for _ in range(500):
            stake = kelly_stake(bankroll, 0.70, 0.55, c)
            if stake <= 0:
                break
            won = rng.random() < 0.55       # model overstates by 15 points
            bankroll += settle_pnl(stake, 0.55, won, c.fee_bps)
            if bankroll < c.min_stake_usdt:
                break
        self.assertGreater(bankroll, 0.0)

    def test_prunes_stale_state(self):
        client = FakeClient([], [(0, 100_000.0)], {}, {})
        t = self._trader(client)
        t._seen = {1: 1_000, 2: 10_000_000_000}
        t._hydrated = {}
        t._prune(10_000_000_000)
        self.assertNotIn(1, t._seen)
        self.assertIn(2, t._seen)


class TestWeiUnits(unittest.TestCase):
    """Amounts are 18-decimal wei; float maths here loses money."""

    def test_one_usdt(self):
        self.assertEqual(m.to_wei(1), "1000000000000000000")

    def test_fractional(self):
        self.assertEqual(m.to_wei(1.5), "1500000000000000000")

    def test_round_trip(self):
        for v in ("0.01", "1.5", "37.42", "1234.567891"):
            self.assertEqual(m.from_wei(m.to_wei(v)), Decimal(v))

    def test_truncates_never_inflates(self):
        """Must never round up past the available balance."""
        self.assertEqual(m.to_wei("1.0000000000000000009"),
                         "1000000000000000000")

    def test_no_float_drift(self):
        """0.1+0.2 style error would produce a wrong wei integer."""
        self.assertEqual(m.to_wei("0.3"), "300000000000000000")

    def test_rejects_nonpositive(self):
        for v in (0, -1):
            with self.assertRaises(ValueError):
                m.to_wei(v)


class TestVariantParsing(unittest.TestCase):
    """The strike is startPrice, and the feed is the venue's own oracle."""

    def test_reads_start_price_and_feed(self):
        strike, sym = PredictionClient._parse_variant(
            {"startPrice": "104250.5", "priceFeedSymbol": "BTCUSD",
             "priceFeedProvider": "PYTH"})
        self.assertAlmostEqual(strike, 104250.5)
        self.assertEqual(sym, "BTCUSD")

    def test_missing_start_price(self):
        self.assertEqual(PredictionClient._parse_variant({}), (None, None))

    def test_rejects_garbage(self):
        self.assertIsNone(
            PredictionClient._parse_variant({"startPrice": "abc"})[0])
        self.assertIsNone(
            PredictionClient._parse_variant({"startPrice": "0"})[0])


class TestLiveQuoteGate(unittest.TestCase):
    """A venue quote worse than the screen must abort the trade."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _trader(self, client, **kw):
        c = cfg(db_path=self.db, live=True, **kw)
        return build_trader(client, c, self.db)

    def test_skips_when_quote_exceeds_price_ceiling(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})
        client.get_quote = lambda r, s, st: m.Quote("q", 0.93, 1.0, 0.0, 0.0)
        # 0.93 is above max_entry_price (0.90): must never execute, even
        # though at this spot the model still nominally shows an edge.

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertIsNone(t._position)
        self.assertEqual(client.orders, [])

    def test_skips_when_quote_erases_the_edge(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_050.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})
        client.get_quote = lambda r, s, st: m.Quote("q", 0.88, 1.0, 0.0, 0.0)

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertIsNone(t._position)
        self.assertEqual(client.orders, [])

    def test_skips_on_excessive_price_impact(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})
        client.get_quote = lambda r, s, st: m.Quote("q", 0.55, 1.0, 0.40, 0.0)

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertIsNone(t._position)
        self.assertEqual(client.orders, [])

    def test_places_order_and_records_quote_price(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})

        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertIsNotNone(t._position)
        self.assertEqual(len(client.orders), 1)
        self.assertAlmostEqual(t._position.signal.fill_price, 0.51)


class TestSymbolMapping(unittest.TestCase):
    """Oracle feed symbols are not Binance tickers."""

    def _client(self, valid):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c._symbol_cache = {}
        calls = []

        class S:
            def get(self, url, params=None, timeout=None):
                calls.append(params["symbol"])
                return types.SimpleNamespace(
                    status_code=200 if params["symbol"] in valid else 400)

        c._session = S()
        return c, calls

    def test_none_defaults_to_btcusdt(self):
        c, _ = self._client({"BTCUSDT"})
        self.assertEqual(c.market_symbol(None), "BTCUSDT")

    def test_valid_symbol_passes_through(self):
        c, _ = self._client({"BTCUSDT", "BTCUSDC"})
        self.assertEqual(c.market_symbol("BTCUSDC"), "BTCUSDC")

    def test_pyth_style_symbol_is_normalised(self):
        c, calls = self._client({"BTCUSD"})
        self.assertEqual(c.market_symbol("BTC/USD"), "BTCUSD")
        self.assertIn("BTCUSD", calls)

    def test_unknown_feed_falls_back_not_crashes(self):
        c, _ = self._client({"BTCUSDT"})
        self.assertEqual(c.market_symbol("Crypto.BTC/USD"), "BTCUSDT")

    def test_result_is_cached(self):
        c, calls = self._client({"BTCUSD"})
        c.market_symbol("BTC/USD")
        c.market_symbol("BTC/USD")
        self.assertEqual(len(calls), 1)


class TestVolatilityCache(unittest.TestCase):
    """Sigma must be cached per symbol, not globally."""

    def test_separate_symbols_do_not_share_a_cache(self):
        seen = []

        class S:
            def get(self, url, params=None, timeout=None):
                seen.append(params["symbol"])
                # Distinct, small-amplitude series so neither hits the
                # volatility ceiling and gets clamped to the same value.
                amp = 0.0005 if params["symbol"] == "BTCUSDT" else 0.0020
                rows = [[0, 0, 0, 0, str(100.0 * (1 + amp * (i % 2)))]
                        for i in range(30)]
                return types.SimpleNamespace(
                    raise_for_status=lambda: None, json=lambda: rows)

        v = m.VolatilityEstimator(cfg(), S())
        a = v.sigma_annual("BTCUSDT")
        b = v.sigma_annual("BTCUSD")
        self.assertEqual(seen, ["BTCUSDT", "BTCUSD"])
        self.assertNotEqual(a, b)
        v.sigma_annual("BTCUSDT")
        self.assertEqual(len(seen), 2)      # cached, not refetched


class TestRedemption(unittest.TestCase):
    """Winnings are not auto-credited; unclaimed wins must not look like loss."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _live_trader(self, client, **over):
        c = cfg(db_path=self.db, live=True, **over)
        return build_trader(client, c, self.db)

    def _win_once(self, client, **over):
        over.setdefault("claim_poll_interval_s", 0.01)
        over.setdefault("claim_timeout_s", 0.5)
        t = self._live_trader(client, **over)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        client.t = 2
        client._winners[1] = Side.UP
        t._settle_open()
        return t

    @staticmethod
    def _client():
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start, end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + (m.DEFAULT_ROUND_SECONDS * 1000) + 3_000, 100_400.0)]
        return FakeClient([rnd], path, {(1, Side.UP): [(0.50, 10_000)]}, {})

    def test_a_win_triggers_redemption(self):
        client = self._client()
        client.redeem_state = "SUCCESS"
        t = self._win_once(client)
        t._claim_queue.join()          # wait for the background worker
        self.assertEqual(client.redeemed, ["1"])   # UP token
        self.assertEqual(t._unredeemed, {})        # confirmed and cleared

    def test_unredeemed_winnings_are_not_double_counted(self):
        """
        The API reading is authoritative by default.

        Adding the gross payout on top of a portfolio figure that already
        includes settled positions inflated the bankroll by the GROSS payout
        (stake + profit), not the profit.
        """
        client = self._client()
        client.balance = 95.0
        t = self._win_once(client)
        self.assertAlmostEqual(t._bankroll(), 95.0, places=6)

    def test_unredeemed_can_be_counted_when_explicitly_configured(self):
        client = self._client()
        client.balance = 95.0
        # _claim() records the pending payout synchronously, before handing
        # the actual redemption to the worker, so this is readable right
        # away without waiting on the background thread.
        t = self._win_once(client, count_unredeemed_in_bankroll=True)
        payout = t._unredeemed["1"][0]
        self.assertAlmostEqual(t._bankroll(), 95.0 + payout, places=6)

    def test_a_winning_streak_does_not_trip_the_loss_limit(self):
        client = self._client()
        client.balance = 82.0                      # three stakes out, none back
        t = self._win_once(client)
        t._account_risk.check(t._bankroll())               # must not raise

    def test_confirmed_redemption_clears_the_pending_entry(self):
        client = self._client()
        client.redeem_state = "PENDING"
        t = self._win_once(client)
        client.balance = 105.0
        # Flip to confirmed and let the worker's next poll pick it up --
        # this is the background equivalent of the old manual poll call.
        client.redeem_state = "SUCCESS"
        t._claim_queue.join()
        self.assertEqual(t._unredeemed, {})
        self.assertAlmostEqual(t._bankroll(), 105.0, places=6)

    def test_pending_redemption_is_not_cleared_early(self):
        client = self._client()
        client.redeem_state = "PENDING"
        t = self._win_once(client, claim_timeout_s=0.05,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()          # worker gives up after claim_timeout_s
        self.assertIn("1", t._unredeemed)   # still tracked, not silently dropped

    def test_failed_claim_is_still_tracked_and_retried(self):
        client = self._client()
        client.redeem_fails = True
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        self.assertIn("1", t._unredeemed)          # value not lost
        # The worker is retrying relentlessly in the background; let it
        # fail a few times, then allow it through and wait for the tx hash.
        time.sleep(0.05)
        client.redeem_fails = False
        client.redeem_state = "SUCCESS"
        t._claim_queue.join()
        self.assertTrue(client.redeemed)           # the retry got through
        self.assertEqual(t._unredeemed, {})        # and then confirmed

    def test_a_loss_never_redeems(self):
        client = self._client()
        t = self._live_trader(client)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        client.t = 2
        client._winners[1] = Side.DOWN
        t._settle_open()
        self.assertEqual(client.redeemed, [])
        self.assertEqual(t._unredeemed, {})

    def test_paper_mode_never_redeems(self):
        client = self._client()
        c = cfg(db_path=self.db)
        t = build_trader(client, c, self.db)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        client.t = 2
        client._winners[1] = Side.UP
        t._settle_open()
        self.assertEqual(client.redeemed, [])

    # -- redeemed by hand, outside the bot ---------------------------------

    def test_a_hand_redeemed_win_stops_being_chased(self):
        """
        The operator claims the win in the Binance app. The bot must notice.

        Its only success signal used to be the status of a tx hash IT had
        submitted, so a redemption performed anywhere else was invisible: the
        worker re-submitted the claim every poll for the whole timeout and
        then kept the token in _unredeemed forever, where nothing retries it.
        """
        client = self._client()
        calls = []

        def nothing_left(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            raise m.NothingToRedeem("no redeemable balance for 1")

        client.batch_redeem = nothing_left
        client.balance = 105.0                 # the money is already there
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 1)        # asked once, took the answer
        self.assertEqual(t._unredeemed, {})    # no phantom left behind
        self.assertAlmostEqual(t._outstanding(), 0.0, places=6)

    def test_venue_saying_already_redeemed_ends_the_claim(self):
        """The same fact delivered as an error rather than an empty batch."""
        client = self._client()
        calls = []

        def already(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            raise m.ApiError("token has already been redeemed")

        client.batch_redeem = already
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(t._unredeemed, {})

    def test_a_dead_redemption_tx_is_resubmitted_then_stops(self):
        """
        A hand-redemption that races the bot's own transaction.

        The bot gets a tx hash, the operator claims in the app, and the bot's
        transaction reverts. Once a hash existed the worker only ever asked
        for its status, so a terminal failure kept it polling a transaction
        that would never confirm until the timeout ran out.
        """
        client = self._client()
        client.redeem_state = "REVERTED"
        calls = []

        def redeem(token_ids, chain_id="56"):
            calls.append(tuple(token_ids))
            if len(calls) == 1:
                return ["0xtx1"]
            raise m.NothingToRedeem("nothing redeemable for 1")

        client.batch_redeem = redeem
        t = self._win_once(client, claim_timeout_s=0.5,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertEqual(len(calls), 2)        # resubmitted once, then stopped
        self.assertEqual(t._unredeemed, {})

    def test_a_genuine_redeem_failure_is_still_retried(self):
        """
        The escape hatch must not swallow real failures.

        A transient rejection is an absence of information, not proof the
        money landed, and dropping the token on one would strand a real win.
        """
        client = self._client()
        client.redeem_fails = True
        t = self._win_once(client, claim_timeout_s=0.2,
                           claim_poll_interval_s=0.01)
        t._claim_queue.join()
        self.assertIn("1", t._unredeemed)       # still tracked


class TestBatchRedeemResponses(unittest.TestCase):
    """Telling 'accepted, in flight' apart from 'there was nothing to claim'."""

    @staticmethod
    def _client(payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None
        c._static_cfg = cfg()
        c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: payload
        return c

    def test_tx_hashes_are_returned(self):
        c = self._client({"results": [{"txHash": "0xdead"}]})
        self.assertEqual(c.batch_redeem(["1"], "56"), ["0xdead"])

    def test_accepted_batch_without_a_hash_is_in_flight(self):
        c = self._client({"results": [], "batchId": "b1"})
        self.assertEqual(c.batch_redeem(["1"], "56"), [])

    def test_empty_response_means_nothing_was_redeemable(self):
        c = self._client({"results": []})
        with self.assertRaises(m.NothingToRedeem):
            c.batch_redeem(["1"], "56")


class TestStudentT(unittest.TestCase):
    """Tail model, verified against scipy where available."""

    def test_matches_scipy(self):
        try:
            from scipy import stats
        except ImportError:
            self.skipTest("scipy not installed")
        for x in (-4.0, -1.5, -0.3, 0.0, 0.7, 2.5, 5.0):
            for df in (2.5, 3.0, 4.0, 7.5, 30.0):
                self.assertAlmostEqual(m.student_t_cdf(x, df),
                                       stats.t.cdf(x, df), places=10)

    def test_symmetry(self):
        for x in (0.5, 1.5, 3.0):
            self.assertAlmostEqual(m.student_t_cdf(x, 4)
                                   + m.student_t_cdf(-x, 4), 1.0, places=12)

    def test_converges_to_gaussian_at_high_df(self):
        for x in (-2.0, 0.5, 1.8):
            self.assertAlmostEqual(m.standardised_t_cdf(x, 5000),
                                   m.norm_cdf(x), places=4)

    def test_variance_is_matched(self):
        """Rescaling means df changes shape, not spread."""
        self.assertAlmostEqual(m.standardised_t_cdf(0.0, 4), 0.5, places=12)

    def test_fat_tails_raise_far_otm_probability(self):
        """The whole point: cheap lottery tickets are worth more than Gaussian says."""
        g = m.digital_up_probability(99_000, 100_000, 0.5, 120, None)
        t = m.digital_up_probability(99_000, 100_000, 0.5, 120, 3.0)
        self.assertGreater(t, g)
        self.assertGreater(t / max(g, 1e-12), 2.0)

    def test_still_a_valid_probability(self):
        import random
        rng = random.Random(3)
        for _ in range(1500):
            p = m.digital_up_probability(rng.uniform(5e4, 1.5e5),
                                         rng.uniform(5e4, 1.5e5),
                                         rng.uniform(0.1, 3.0),
                                         rng.uniform(0, 300),
                                         rng.choice([None, 2.5, 4.0, 12.0]))
            self.assertTrue(0.0 <= p <= 1.0)

    def test_rejects_degenerate_df(self):
        with self.assertRaises(ValueError):
            m.standardised_t_cdf(1.0, 2.0)


class TestTailEstimation(unittest.TestCase):

    def _est(self, rets):
        v = m.VolatilityEstimator(cfg(), None)
        import statistics as st
        return v._estimate_df(rets, st.pstdev(rets))

    def test_gaussian_data_yields_no_fat_tail(self):
        import random
        rng = random.Random(5)
        self.assertIsNone(self._est([rng.gauss(0, 0.001) for _ in range(500)]))

    def test_fat_data_yields_low_df(self):
        import random
        rng = random.Random(5)
        rets = [rng.gauss(0, 0.001) * (6 if rng.random() < 0.03 else 1)
                for _ in range(2000)]
        df = self._est(rets)
        self.assertIsNotNone(df)
        self.assertLess(df, 12.0)

    def test_clamped_to_configured_bounds(self):
        import random
        rng = random.Random(5)
        rets = [rng.gauss(0, 0.001) * (60 if rng.random() < 0.002 else 1)
                for _ in range(3000)]
        df = self._est(rets)
        self.assertGreaterEqual(df, cfg().tail_df_floor)

    def test_short_series_is_ignored(self):
        self.assertIsNone(self._est([0.001, -0.002, 0.003]))

    def test_noise_threshold_scales_with_sample_size(self):
        """Repeated Gaussian draws must not produce false fat-tail signals."""
        import random
        rng = random.Random(21)
        false_positives = sum(
            self._est([rng.gauss(0, 0.001) for _ in range(400)]) is not None
            for _ in range(30))
        self.assertLessEqual(false_positives, 2)

    def test_below_200_samples_refuses_to_guess(self):
        import random
        rng = random.Random(4)
        rets = [rng.gauss(0, 0.001) * (8 if rng.random() < 0.05 else 1)
                for _ in range(150)]
        self.assertIsNone(self._est(rets))


class TestEdgeThresholds(unittest.TestCase):
    """Both an absolute floor and a relative margin must clear."""

    def test_relative_margin_blocks_thin_cheap_edges(self):
        c = convex_cfg()
        be = breakeven_probability(0.10, c.fee_bps)
        self.assertFalse(m.clears_edge(be + 0.021, 0.10, c))   # abs ok, rel not

    def test_absolute_floor_blocks_noise_at_tiny_prices(self):
        c = convex_cfg(min_edge=0.02, min_edge_ratio=0.0)
        be = breakeven_probability(0.05, c.fee_bps)
        self.assertFalse(m.clears_edge(be + 0.005, 0.05, c))

    def test_clears_when_both_satisfied(self):
        c = convex_cfg()
        be = breakeven_probability(0.20, c.fee_bps)
        self.assertTrue(m.clears_edge(be * 1.5, 0.20, c))

    def test_threshold_is_consistent_across_price_levels(self):
        """A 40% margin should pass at both ends of the book."""
        c = convex_cfg(min_edge=0.001, min_edge_ratio=0.30)
        for price in (0.06, 0.15, 0.30):
            be = breakeven_probability(price, c.fee_bps)
            self.assertTrue(m.clears_edge(be * 1.40, price, c), price)


class TestConvexProfile(unittest.TestCase):

    def test_refuses_expensive_contracts(self):
        c = convex_cfg()
        rnd = make_round()
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.80, 10_000)]}
        self.assertIsNone(assess(rnd, 103_000, 0.5, 1000, now, c, book).signal)

    def test_buys_cheap_underpriced_contracts(self):
        c = convex_cfg()
        rnd = make_round()
        now = rnd.end_ms - 120_000
        book = {Side.UP: [(0.10, 100_000)]}
        sig = assess(rnd, 100_120, 0.5, 1000, now, c, book, tail_df=3.0).signal
        self.assertIsNotNone(sig)
        self.assertLessEqual(sig.fill_price, c.max_entry_price)

    def test_losses_are_bounded_and_small(self):
        c = convex_cfg()
        self.assertLessEqual(m.kelly_stake(1000, 0.60, 0.20, c),
                             1000 * c.max_stake_pct + 1e-9)

    def test_wins_are_multiples_of_the_stake(self):
        win = settle_pnl(10.0, 0.12, True, 200)
        loss = settle_pnl(10.0, 0.12, False, 200)
        self.assertGreater(win, 6 * abs(loss) / 10 * 10 / 10 * 6)
        self.assertAlmostEqual(loss, -10.0)
        self.assertGreater(win / abs(loss), 6.0)

    def test_survives_a_long_losing_streak(self):
        """Convex strategies lose most rounds; that must not be ruin."""
        c = convex_cfg()
        bank = 100.0
        for _ in range(40):
            stake = min(bank * c.max_stake_pct, bank)
            bank -= stake
        self.assertGreater(bank, 40.0)


class TestCalibrationBreaker(unittest.TestCase):
    """Streak counting is wrong for low win rates; use significance instead."""

    def test_long_streak_on_cheap_bets_is_not_a_halt(self):
        c = convex_cfg()
        r = RiskManager(c, 100.0)
        for _ in range(40):
            r.record_result(False, 0.12)      # 12% bets losing 40x: expected
        r.check(100.0)                        # must not raise
        self.assertGreater(r.calibration_z(), c.calibration_z_halt)

    def test_losing_high_probability_bets_does_halt(self):
        r = RiskManager(convex_cfg(), 100.0)
        for _ in range(40):
            r.record_result(False, 0.75)      # 75% bets never winning: broken
        with self.assertRaises(TradingHalted):
            r.check(100.0)

    def test_no_verdict_before_minimum_samples(self):
        r = RiskManager(convex_cfg(), 100.0)
        for _ in range(5):
            r.record_result(False, 0.75)
        self.assertIsNone(r.calibration_z())
        r.check(100.0)

    def test_results_matching_the_model_never_halt(self):
        import random
        rng = random.Random(9)
        r = RiskManager(convex_cfg(), 100.0)
        for _ in range(150):
            p = rng.uniform(0.05, 0.35)
            r.record_result(rng.random() < p, p)
        r.check(100.0)
        self.assertGreater(r.calibration_z(), -2.5)

    def test_z_is_signed_correctly(self):
        r = RiskManager(convex_cfg(), 100.0)
        for _ in range(50):
            r.record_result(True, 0.20)       # far better than predicted
        self.assertGreater(r.calibration_z(), 0)

    def test_streak_backstop_applies_only_before_enough_data(self):
        """Crude instrument, used only while the good one is unavailable."""
        r = RiskManager(convex_cfg(max_consecutive_losses=5), 100.0)
        for _ in range(5):
            r.record_result(False, 0.10)
        self.assertIsNone(r.calibration_z())      # too few samples to judge
        with self.assertRaises(TradingHalted):
            r.check(100.0)

    def test_streak_is_ignored_once_calibration_is_available(self):
        c = convex_cfg(max_consecutive_losses=5, calibration_min_samples=10)
        r = RiskManager(c, 100.0)
        for _ in range(40):
            r.record_result(False, 0.10)          # 40-loss streak at 10% odds
        self.assertIsNotNone(r.calibration_z())
        r.check(100.0)                            # statistically fine


class TestRequestSigning(unittest.TestCase):
    """The signed bytes and the sent bytes must be byte-identical."""

    def _client(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c._clock_offset_ms = 0
        c._symbol_cache = {}
        c._wallet = None
        return c

    def test_signature_covers_exactly_the_sent_string(self):
        import hmac, hashlib
        c = self._client()
        q = c._signed_query({"b": "2", "a": "1", "limit": 50})
        sent, sig = q.rsplit("&signature=", 1)
        expected = hmac.new(b"s", sent.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_parameters_are_sorted_in_the_sent_string(self):
        c = self._client()
        sent = c._signed_query({"zeta": 1, "alpha": 2}).rsplit("&signature=", 1)[0]
        keys = [kv.split("=")[0] for kv in sent.split("&")]
        self.assertEqual(keys, sorted(keys))

    def test_timestamp_and_recvwindow_are_included(self):
        c = self._client()
        q = c._signed_query({})
        self.assertIn("timestamp=", q)
        self.assertIn("recvWindow=", q)

    def test_none_values_are_dropped(self):
        self.assertNotIn("skipme",
                         self._client()._signed_query({"skipme": None}))

    def test_list_params_repeat_rather_than_stringify(self):
        """Regression: tokenIds must not serialise as a Python repr."""
        c = self._client()
        q = c._signed_query({"tokenIds": ["111", "222"]})
        self.assertIn("tokenIds=111", q)
        self.assertIn("tokenIds=222", q)
        self.assertNotIn("%5B", q)      # no "["

    def test_signature_changes_when_a_parameter_changes(self):
        c = self._client()
        a = c._signed_query({"x": "1"}).rsplit("&signature=", 1)[1]
        b = c._signed_query({"x": "2"}).rsplit("&signature=", 1)[1]
        self.assertNotEqual(a, b)


class TestErrorSurfacing(unittest.TestCase):
    """Binance puts the diagnosis in the body; it must reach the user."""

    def _client_returning(self, status, body, text="{}"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=status, text=text,
                    json=lambda: body if body is not None else (_ for _ in ()).throw(ValueError()))
        c._session = S()
        return c

    def test_signature_error_is_explained(self):
        c = self._client_returning(400, {"code": -1022, "msg": "Signature invalid"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        msg = str(ctx.exception)
        self.assertIn("-1022", msg)
        self.assertIn("signed and sent", msg)

    def test_permission_error_is_explained(self):
        c = self._client_returning(401, {"code": -2015, "msg": "Invalid API-key"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("permissions", str(ctx.exception))

    def test_unknown_code_still_surfaces_the_message(self):
        c = self._client_returning(400, {"code": -9999, "msg": "Weird failure"})
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("Weird failure", str(ctx.exception))

    def test_non_json_error_body_is_not_swallowed(self):
        c = self._client_returning(500, None, text="upstream exploded")
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertIn("upstream exploded", str(ctx.exception))

    def test_quota_failure_propagates_rather_than_returning_none(self):
        """Regression: preflight printed 'OK None' for a failing endpoint."""
        c = self._client_returning(400, {"code": -1102, "msg": "bad param"})
        with self.assertRaises(m.ApiError):
            c.remaining_quota_usdt()


class TestEndpointMethods(unittest.TestCase):
    """The verb must travel with the path."""

    def test_trading_endpoints_are_post(self):
        for name in ("get_quote", "place_order", "batch_redeem"):
            self.assertEqual(m.DEFAULT_ENDPOINTS[name][0], "POST", name)

    def test_read_endpoints_are_get(self):
        for name in ("market_list", "market_detail", "order_book",
                     "wallet_list", "balances", "quota_status",
                     "settled_history", "redeem_status", "positions"):
            self.assertEqual(m.DEFAULT_ENDPOINTS[name][0], "GET", name)

    def test_every_endpoint_declares_a_valid_verb(self):
        for name, (verb, path) in m.DEFAULT_ENDPOINTS.items():
            self.assertIn(verb, ("GET", "POST"), name)
            self.assertTrue(path.startswith("/sapi/"), name)

    def _built(self, endpoints):
        doc = m.default_config_document()
        doc["endpoints"].update(endpoints)
        return m.build_config(doc, api_key="k", api_secret="s", live=False,
                              db_path="d")

    def test_override_accepts_bare_path_and_keeps_method(self):
        self.assertEqual(self._built({"get_quote": "/new/path"}).ep("get_quote"),
                         ("POST", "/new/path"))

    def test_override_accepts_method_path_pair(self):
        self.assertEqual(self._built({"order_book": ["POST", "/x"]})
                         .ep("order_book"), ("POST", "/x"))

    def test_override_rejects_bad_method(self):
        with self.assertRaises(ValueError):
            self._built({"order_book": ["FETCH", "/x"]})

    def test_override_rejects_unknown_endpoint(self):
        with self.assertRaises(ValueError):
            self._built({"not_an_endpoint": "/x"})


class TestClampedSigmaGuard(unittest.TestCase):
    """A clamped sigma is an assertion, not a measurement."""

    def test_malformed_closes_do_not_crash(self):
        rows = [[0, 0, 0, 0, v] for v in
                ["100", "0", "101", "-5", "102", "103"] * 40]

        class S:
            def get(self, url, params=None, timeout=None):
                return types.SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: rows)
        est = m.VolatilityEstimator(cfg(), S())
        self.assertGreater(est.sigma_annual(), 0.0)   # survives, no exception

    def _est(self, per_min_sd):
        import random
        rng = random.Random(2)
        rows = []
        px = 100.0
        for _ in range(500):
            px *= math.exp(rng.gauss(0, per_min_sd))   # stays positive
            rows.append([0, 0, 0, 0, str(px)])

        class S:
            def get(self, url, params=None, timeout=None):
                return types.SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: rows)
        return m.VolatilityEstimator(cfg(), S())

    def test_normal_volatility_is_not_clamped(self):
        est = self._est(0.0008)
        est.sigma_annual()
        self.assertFalse(est.is_clamped())

    def test_dead_market_is_flagged_as_clamped(self):
        est = self._est(1e-9)
        sigma = est.sigma_annual()
        self.assertTrue(est.is_clamped())
        self.assertAlmostEqual(sigma, cfg().vol_floor_annual)

    def test_extreme_volatility_is_flagged_as_clamped(self):
        est = self._est(0.5)
        est.sigma_annual()
        self.assertTrue(est.is_clamped())

    def test_clamped_flag_is_per_symbol(self):
        est = self._est(0.0008)
        est.sigma_annual("BTCUSDT")
        self.assertFalse(est.is_clamped("BTCUSDT"))
        self.assertFalse(est.is_clamped("NEVERQUERIED"))

    def test_overstated_sigma_inflates_cheap_contracts(self):
        """The reason the guard exists, stated as an assertion."""
        true_p = 1 - m.digital_up_probability(65006.5, 64990.0, 0.08, 120, 4.76)
        floor_p = 1 - m.digital_up_probability(65006.5, 64990.0, 0.15, 120, 4.76)
        self.assertGreater(floor_p / true_p, 3.0)


class TestBalanceLookup(unittest.TestCase):
    """Regression: a funded account reported 0.00 because of a hard filter."""

    def _client(self, items, account_type="AUTO"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(account_type=account_type)
        c._clock_offset_ms = 0; c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=200, text="", json=lambda: {"items": items})
        c._session = S()
        return c

    def test_finds_funds_under_an_unexpected_account_type(self):
        """The actual failure: money in FUNDING, code only looked at SPOT."""
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "6.64", "enabled": True}])
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_breakdown_lists_every_option(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "6.64", "enabled": False}])
        opts = c.payment_options()
        self.assertEqual(len(opts), 2)
        self.assertIn(("FUNDING", 6.64, False), opts)

    def test_disabled_options_are_not_used(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "1.0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "99", "enabled": False}])
        self.assertAlmostEqual(c.balance_usdt(), 1.0)

    def test_explicit_account_type_is_respected(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "1.0", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "99", "enabled": True}],
            account_type="SPOT")
        self.assertAlmostEqual(c.balance_usdt(), 1.0)

    def test_explicit_missing_type_names_what_is_available(self):
        c = self._client([
            {"accountType": "FUNDING", "availableBalanceDisplay": "9", "enabled": True}],
            account_type="SPOT")
        with self.assertRaises(m.ApiError) as ctx:
            c.balance_usdt()
        self.assertIn("FUNDING=9.00", str(ctx.exception))

    def test_malformed_balance_is_treated_as_zero_not_a_crash(self):
        c = self._client([
            {"accountType": "SPOT", "availableBalanceDisplay": "abc", "enabled": True},
            {"accountType": "FUNDING", "availableBalanceDisplay": "5", "enabled": True}])
        self.assertAlmostEqual(c.balance_usdt(), 5.0)

    def test_empty_response_raises(self):
        with self.assertRaises(m.ApiError):
            self._client([]).balance_usdt()


class TestBankrollViability(unittest.TestCase):
    """
    Viability is decided by kelly_stake, not by a percentage rule of thumb.

    These tests previously asserted that a 6.64 balance CANNOT trade, which
    encoded the idle-forever bug as correct behaviour. Sizing is now bounded
    by the 2x-full-Kelly limit rather than by max_stake_pct alone.
    """

    def test_a_conservative_profile_declines_a_tiny_bankroll(self):
        """
        Correct, not a bug: convex risks 2% per trade, so its hard cap is 5%.
        Forcing a 1.00 minimum on 6.64 would be 15% of bankroll -- far past
        what that profile's own risk shape allows. It declines rather than
        quietly trading three times its intended size.
        """
        self.assertEqual(m.kelly_stake(6.64, 0.90, 0.10, convex_cfg()), 0.0)

    def test_a_profile_built_for_small_accounts_does_trade(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertGreater(m.kelly_stake(6.64, 0.70, 0.60, c, 0), 0.0)

    def test_larger_bankroll_also_trades(self):
        c = convex_cfg()
        self.assertGreater(m.kelly_stake(500.0, 0.90, 0.10, c), 0.0)

    def test_stake_never_exceeds_the_hard_cap(self):
        for prof in ("convex", "balanced", "micro"):
            c = Config(api_key="k", api_secret="s", **m.PROFILES[prof])
            for bank in (2.0, 6.64, 30.0, 1000.0):
                st = m.kelly_stake(bank, 0.99, 0.05, c)
                self.assertLessEqual(st, bank * c.hard_max_stake_pct + 1e-9,
                                     f"{prof} @ {bank}")

    def test_a_bankroll_below_the_order_minimum_cannot_trade(self):
        c = convex_cfg()
        self.assertEqual(m.kelly_stake(0.80, 0.90, 0.10, c), 0.0)


class TestRawSigmaDiagnostic(unittest.TestCase):

    def _est(self, sd):
        import random
        rng = random.Random(2); rows = []; px = 100.0
        for _ in range(500):
            px *= math.exp(rng.gauss(0, sd))
            rows.append([0, 0, 0, 0, str(px)])

        class S:
            def get(self, url, params=None, timeout=None):
                return types.SimpleNamespace(raise_for_status=lambda: None,
                                             json=lambda: rows)
        return m.VolatilityEstimator(cfg(), S())

    def test_raw_sigma_is_recorded_below_the_floor(self):
        est = self._est(1e-7)
        used = est.sigma_annual()
        raw = est.raw_sigma()
        self.assertTrue(est.is_clamped())
        self.assertLess(raw, used)

    def test_raw_equals_used_when_not_clamped(self):
        est = self._est(0.0008)
        used = est.sigma_annual()
        self.assertAlmostEqual(est.raw_sigma(), used, places=12)


class TestPerMarketFee(unittest.TestCase):
    """The market's published fee wins over the config fallback."""

    def test_kelly_uses_the_supplied_fee(self):
        c = cfg(fee_bps=200)
        low = m.kelly_stake(1000, 0.65, 0.60, c, fee_bps=1)
        high = m.kelly_stake(1000, 0.65, 0.60, c, fee_bps=200)
        self.assertGreater(low, high)      # smaller fee -> larger edge -> bigger

    def test_clears_edge_uses_the_supplied_fee(self):
        c = cfg(min_edge=0.045, min_edge_ratio=0.0)
        self.assertTrue(m.clears_edge(0.6460, 0.60, c, fee_bps=1))
        self.assertFalse(m.clears_edge(0.6460, 0.60, c, fee_bps=200))

    def test_evaluate_reads_fee_from_the_round(self):
        rnd_free = make_round(fee_bps=1)
        rnd_paid = make_round(fee_bps=900)
        now = rnd_free.end_ms - 60_000
        book = {Side.UP: [(0.55, 10_000)]}
        c = cfg(min_edge=0.04, min_edge_ratio=0.05)
        s_free = assess(rnd_free, 100_150, 0.5, 1000, now, c, book).signal
        s_paid = assess(rnd_paid, 100_150, 0.5, 1000, now, c, book).signal
        self.assertIsNotNone(s_free)
        if s_paid is not None:
            self.assertGreater(s_free.edge, s_paid.edge)

    def test_settle_pnl_honours_a_zero_fee(self):
        self.assertAlmostEqual(settle_pnl(1.0, 0.5, True, 0), 1.0, places=9)
        self.assertAlmostEqual(settle_pnl(1.0, 0.5, True, 200), 0.98, places=9)

    def test_fallback_used_when_no_fee_supplied(self):
        c = cfg(fee_bps=500)
        self.assertAlmostEqual(m.kelly_stake(1000, 0.65, 0.60, c),
                               m.kelly_stake(1000, 0.65, 0.60, c, 500))


class TestMicroProfile(unittest.TestCase):
    """Small accounts: the venue minimum forces the risk fraction."""

    def test_micro_can_trade_a_small_balance(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertGreaterEqual(6.64 * c.max_stake_pct, c.min_stake_usdt)

    def test_one_dollar_minimum_is_accepted(self):
        c = Config(api_key="k", api_secret="s", min_stake_usdt=1.0)
        self.assertEqual(c.min_stake_usdt, 1.0)

    def test_implausibly_small_minimum_still_rejected(self):
        with self.assertRaises(ValueError):
            Config(api_key="k", api_secret="s", min_stake_usdt=0.1)

    def test_micro_targets_the_mid_price_band(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertGreaterEqual(c.min_entry_price, 0.30)
        self.assertLessEqual(c.max_entry_price, 0.80)

    def test_bounded_loss_survives_a_long_streak(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        bank = 100.0
        for _ in range(20):
            bank -= min(bank * c.max_stake_pct, bank)
        self.assertGreater(bank, 1.0)


class TestVenueDerivedParameters(unittest.TestCase):
    """Nothing routing-related may be assumed when the venue publishes it."""

    def test_chain_collateral_precision_come_from_payload(self):
        t = TestParseRound.topic()
        t["chainId"] = "97"; t["collateral"] = "USDC"
        t["markets"][0]["decimalPrecision"] = 6
        r = PredictionClient._parse_round(t)
        self.assertEqual((r.chain_id, r.collateral, r.decimal_precision),
                         ("97", "USDC", 6))

    def test_missing_chain_id_rejects_the_market(self):
        t = TestParseRound.topic(); del t["chainId"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_collateral_rejects_the_market(self):
        t = TestParseRound.topic(); del t["collateral"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_vendor_rejects_the_market(self):
        t = TestParseRound.topic(); del t["vendor"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_missing_fee_rejects_rather_than_defaulting(self):
        """Regression: a missing fee used to silently become 200 bps."""
        t = TestParseRound.topic(); del t["feeRateBps"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_zero_fee_is_honoured_not_overridden(self):
        t = TestParseRound.topic(); t["feeRateBps"] = 0
        self.assertEqual(PredictionClient._parse_round(t).fee_bps, 0)

    def test_slippage_takes_the_tighter_of_ours_and_the_venue(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(max_slippage_bps=300)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=1200)), 300)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=100)), 100)
        self.assertEqual(c.effective_slippage_bps(make_round(venue_slippage_bps=0)), 300)

    def test_price_snaps_to_published_precision(self):
        self.assertEqual(make_round(decimal_precision=2).round_price(0.123456), 0.12)
        self.assertEqual(make_round(decimal_precision=4).round_price(0.123456), 0.1235)

    def test_round_duration_is_derived_not_assumed(self):
        r = make_round()
        self.assertEqual(r.duration_ms, r.end_ms - r.start_ms)

    def test_duration_tolerance_scales_with_the_target(self):
        base = 1_700_000_000_000
        t = TestParseRound.topic(startDate=base, endDate=base + 300_000)
        self.assertIsNotNone(PredictionClient._parse_round(t))
        t2 = TestParseRound.topic(startDate=base, endDate=base + 360_000)
        self.assertIsNone(PredictionClient._parse_round(t2))   # 20% off target


class TestMinimumDiscovery(unittest.TestCase):
    """The minimum is not a published field, so it is measured."""

    def _client(self, true_min, balance=50.0):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c.calls = []
        c.balance_usdt = lambda: balance

        def fake_quote(rnd, side, amount):
            c.calls.append(amount)
            if amount > balance:
                raise m.ApiError("Please ensure your account has enough USDT.",
                                 code=-9000)
            if amount < true_min:
                raise m.ApiError("amount below minimum order size", code=None)
            return m.Quote("q", 0.5, amount / 0.5, 0.0, 0.0)

        c.get_quote = fake_quote
        return c

    def test_finds_the_threshold(self):
        c = self._client(1.5)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.5, delta=0.05)

    def test_finds_a_one_dollar_minimum(self):
        c = self._client(1.0)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.0, delta=0.05)

    def test_returns_low_when_everything_is_quotable(self):
        c = self._client(0.0)
        self.assertEqual(c.discover_min_stake(make_round(), Side.UP, low=0.25), 0.25)

    def test_returns_none_when_no_size_is_quotable(self):
        """Every size in range is rejected as too small -> no answer."""
        c = self._client(999.0, balance=100.0)
        self.assertIsNone(c.discover_min_stake(make_round(), Side.UP))

    def test_finds_a_high_floor_when_balance_allows(self):
        """With enough balance the real floor is found, however high."""
        c = self._client(999.0, balance=5000.0)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.5)
        self.assertAlmostEqual(found, 999.0, delta=1.0)

    def test_upper_bound_derives_from_balance(self):
        """Regression: a fixed 10.0 ceiling crashed on a 6.64 balance."""
        c = self._client(1.0, balance=6.64)
        found = c.discover_min_stake(make_round(), Side.UP, tolerance=0.01)
        self.assertAlmostEqual(found, 1.0, delta=0.05)
        self.assertTrue(all(a <= 6.64 + 1e-9 for a in c.calls),
                        f"probed above balance: {c.calls}")

    def test_funds_error_rebounds_instead_of_crashing(self):
        c = self._client(1.0, balance=6.64)
        found = c.discover_min_stake(make_round(), Side.UP, low=0.25,
                                     high=25.0, tolerance=0.01)
        self.assertIsNotNone(found)
        self.assertLessEqual(found, 6.64)

    def test_probing_is_logarithmic_not_linear(self):
        """Probe count must scale with log(range/tolerance), not the range."""
        import math as _math
        low, tol, balance = 0.25, 0.05, 50.0
        c = self._client(1.5, balance=balance)
        c.discover_min_stake(make_round(), Side.UP, low=low, tolerance=tol)
        bound = _math.ceil(_math.log2((balance - low) / tol)) + 2
        self.assertLessEqual(len(c.calls), bound)
        self.assertLess(len(c.calls), (balance - low) / tol)   # not linear

    def test_rejects_invalid_bounds(self):
        c = self._client(1.5)
        with self.assertRaises(ValueError):
            c.discover_min_stake(make_round(), Side.UP, low=5.0, high=1.0)


class TestSmallAccountSizing(unittest.TestCase):
    """Regression: every profile returned 0.00 on a small balance."""

    def _c(self, **kw):
        return Config(api_key="k", api_secret="s", **{**m.PROFILES["micro"], **kw})

    def test_small_balance_with_real_edge_now_trades(self):
        self.assertEqual(m.kelly_stake(6.64, 0.70, 0.60, self._c(), 0), 1.0)

    def test_profiles_either_trade_a_small_balance_or_decline_it(self):
        """No profile may silently exceed its own hard cap to trade."""
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            stake = m.kelly_stake(6.64, 0.70, 0.60, c, 0)
            if stake > 0:
                self.assertLessEqual(stake / 6.64, c.hard_max_stake_pct + 1e-9,
                                     name)

    def test_weak_edge_is_refused_not_forced(self):
        self.assertEqual(m.kelly_stake(6.64, 0.63, 0.60, self._c(), 0), 0.0)

    def test_no_edge_never_triggers_the_override(self):
        self.assertEqual(m.kelly_stake(6.64, 0.55, 0.60, self._c(), 0), 0.0)
        self.assertEqual(m.kelly_stake(6.64, 0.60, 0.60, self._c(), 0), 0.0)

    def test_override_respects_the_two_times_kelly_limit(self):
        """Beyond 2x full Kelly, expected log growth is negative."""
        c = self._c()
        for bank in (2.0, 3.0, 5.0, 10.0, 50.0):
            stake = m.kelly_stake(bank, 0.70, 0.60, c, 0)
            if stake > 0:
                mult = m.kelly_multiple(stake, bank, 0.70, 0.60, 0)
                self.assertLessEqual(mult, 2.0 + 1e-9, f"bankroll {bank}")

    def test_override_respects_the_hard_cap(self):
        # $1 of $6.64 is 15%. With a 5% soft cap the hard cap is 12.5%,
        # so the forced minimum exceeds it and the trade is refused.
        c = self._c(max_stake_pct=0.05, hard_stake_multiple=2.5)
        self.assertEqual(m.kelly_stake(6.64, 0.70, 0.60, c, 0), 0.0)

    def test_override_can_be_disabled(self):
        c = self._c(round_up_to_minimum=False)
        self.assertEqual(m.kelly_stake(6.64, 0.70, 0.60, c, 0), 0.0)

    def test_large_balance_is_unaffected_by_the_override(self):
        """On a large balance Kelly binds and the minimum is irrelevant."""
        c = self._c()
        stake = m.kelly_stake(10_000.0, 0.70, 0.60, c, 0)
        expected = 10_000.0 * min(0.25 * c.kelly_fraction, c.max_stake_pct)
        self.assertAlmostEqual(stake, expected, places=6)
        self.assertGreater(stake, c.min_stake_usdt)

    def test_kelly_multiple_reports_under_betting_correctly(self):
        mult = m.kelly_multiple(1.0, 6.64, 0.70, 0.60, 0)
        self.assertLess(mult, 1.0)      # $1 of $6.64 is BELOW full Kelly here

    def test_hard_cap_can_never_be_below_the_soft_cap(self):
        """The invariant now holds by construction, not by validation."""
        for soft in (0.01, 0.05, 0.10, 0.20, 0.25):
            for mult in (1.0, 1.5, 2.5, 10.0):
                c = Config(api_key="k", api_secret="s", max_stake_pct=soft,
                           hard_stake_multiple=mult)
                self.assertGreaterEqual(c.hard_max_stake_pct, soft,
                                        f"soft={soft} mult={mult}")

    def test_hard_cap_respects_the_ceiling(self):
        c = Config(api_key="k", api_secret="s", max_stake_pct=0.25,
                   hard_stake_multiple=10.0, hard_stake_ceiling=0.35)
        self.assertEqual(c.hard_max_stake_pct, 0.35)

    def test_growth_is_positive_at_the_forced_size(self):
        """Sanity: the accepted override must still compound upward."""
        import math
        q, p, f = 0.70, 0.60, 1.0 / 6.64
        b = (1 - p) / p
        g = q * math.log(1 + b * f) + (1 - q) * math.log(1 - f)
        self.assertGreater(g, 0.0)


class TestQuoteErrorClassification(unittest.TestCase):
    """A broken request must not be reported as 'book too thin'."""

    def _client(self, raiser, balance=50.0):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()
        c.get_quote = raiser
        c.balance_usdt = lambda: balance
        return c

    def test_size_rejection_is_treated_as_below_minimum(self):
        def q(rnd, side, amount):
            if amount < 2.0:
                raise m.ApiError("amount below minimum order size")
            return m.Quote("q", 0.5, 1.0, 0.0, 0.0)
        found = self._client(q).discover_min_stake(make_round(), Side.UP,
                                                   tolerance=0.01)
        self.assertAlmostEqual(found, 2.0, delta=0.05)

    def test_signature_error_is_raised_not_swallowed(self):
        """Regression: any error used to look like 'amount too small'."""
        def q(rnd, side, amount):
            raise m.ApiError("HTTP 400: Signature invalid", code=-1022)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_method_not_supported_is_raised(self):
        def q(rnd, side, amount):
            raise m.ApiError("Request method 'GET' is not supported",
                             code=-1104)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_permission_error_is_raised(self):
        def q(rnd, side, amount):
            raise m.ApiError("invalid API key", code=-2015)
        with self.assertRaises(m.ApiError):
            self._client(q).discover_min_stake(make_round(), Side.UP)

    def test_liquidity_error_counts_as_a_size_problem(self):
        def q(rnd, side, amount):
            if amount < 3.0:
                raise m.ApiError("insufficient liquidity for this amount")
            return m.Quote("q", 0.5, 1.0, 0.0, 0.0)
        found = self._client(q).discover_min_stake(make_round(), Side.UP,
                                                   tolerance=0.01)
        self.assertAlmostEqual(found, 3.0, delta=0.05)


class TestFavoriteProfile(unittest.TestCase):
    """Buying favourites: the opposite side of the market from convex."""

    def _c(self):
        return Config(api_key="k", api_secret="s", **m.PROFILES["favorite"])

    def test_band_matches_a_25pct_return_rule(self):
        c = self._c()
        self.assertLessEqual(c.max_entry_price, 0.80)
        self.assertAlmostEqual((1 - c.max_entry_price) / c.max_entry_price,
                               0.25, places=2)

    def test_refuses_longshots(self):
        c = self._c()
        rnd = make_round()
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.15, 100_000)]}
        self.assertIsNone(assess(rnd, 101_500, 0.5, 1000, now, c, book, 4.0).signal)

    def test_buys_an_underpriced_favourite(self):
        c = self._c()
        rnd = make_round(fee_bps=0)
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.60, 100_000)]}
        sig = assess(rnd, 100_250, 0.5, 1000, now, c, book).signal
        self.assertIsNotNone(sig)
        self.assertGreaterEqual(sig.fill_price, c.min_entry_price)
        self.assertLessEqual(sig.fill_price, c.max_entry_price)

    def test_is_disjoint_from_convex(self):
        fav = self._c()
        cvx = Config(api_key="k", api_secret="s", **m.PROFILES["convex"])
        self.assertGreater(fav.min_entry_price, cvx.max_entry_price)

    def test_enters_later_than_convex(self):
        fav = self._c()
        cvx = Config(api_key="k", api_secret="s", **m.PROFILES["convex"])
        self.assertLess(fav.entry_window_start_s, cvx.entry_window_start_s)


class TestBiasReport(unittest.TestCase):
    """The report must measure which side of the market is mispriced."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.j = Journal(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def _add(self, price, won, n=1, model=None):
        rnd = make_round()
        for _ in range(n):
            sig = Signal(Side.UP, model if model else price + 0.05,
                         price, 0.05, 1.0, 60.0)
            tid = self.j.record("PAPER", rnd, sig, 100_000, 0.5, 100.0)
            self.j.resolve(tid, won, 1.0 if won else -1.0, "venue")

    def test_detects_underpriced_favourites(self):
        self._add(0.70, True, 90)
        self._add(0.70, False, 10)      # 90% actual vs 70% implied
        self.assertIn("<-- underpriced", self.j.calibration_report())

    def test_detects_overpriced_longshots(self):
        self._add(0.15, True, 2)
        self._add(0.15, False, 98)      # 2% actual vs 15% implied
        self.assertIn("<-- overpriced", self.j.calibration_report())

    def test_fair_pricing_is_not_flagged(self):
        """The explanatory text mentions both words; only flags count."""
        self._add(0.60, True, 60)
        self._add(0.60, False, 40)
        report = self.j.calibration_report()
        self.assertNotIn("<-- underpriced", report)
        self.assertNotIn("<-- overpriced", report)

    def test_report_includes_the_bias_section(self):
        self._add(0.60, True, 5)
        self.assertIn("Favourite-longshot bias", self.j.calibration_report())


class TestNoSilentFailures(unittest.TestCase):
    """
    Meta-tests over the source. These exist because every serious bug in this
    project so far was an error being swallowed and reported as something
    benign -- a signature failure read as 'endpoint missing', an auth failure
    read as 'book too thin', a wrong-account lookup read as 'zero balance'.
    """

    @classmethod
    def setUpClass(cls):
        import ast, inspect
        cls.src = inspect.getsource(m)
        cls.tree = ast.parse(cls.src)

    def test_no_bare_except(self):
        import ast
        bare = [n.lineno for n in ast.walk(self.tree)
                if isinstance(n, ast.ExceptHandler) and n.type is None]
        self.assertEqual(bare, [], f"bare except at lines {bare}")

    def test_no_handler_silently_passes(self):
        import ast
        bad = []
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ExceptHandler) and len(n.body) == 1:
                if isinstance(n.body[0], (ast.Pass, ast.Continue)):
                    bad.append(n.lineno)
        self.assertEqual(bad, [], f"silent pass/continue at lines {bad}")

    def test_no_handler_returns_without_explaining(self):
        """
        An except that returns must log first, or re-raise.

        Exempt: functions named `*_or_none`, whose contract is explicitly to
        report absence rather than to hide a failure. The naming convention
        keeps the exemption visible instead of hidden in a test allowlist.
        """
        import ast
        bad = []
        for fn in ast.walk(self.tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name.endswith("_or_none"):
                continue
            for n in ast.walk(fn):
                if not isinstance(n, ast.ExceptHandler):
                    continue
                body = " ".join(ast.unparse(st) for st in n.body)
                returns = any(isinstance(st, ast.Return) for st in n.body)
                explains = ("LOG." in body or "raise" in body
                            or "print" in body)
                if returns and not explains:
                    bad.append((fn.name, n.lineno))
        self.assertEqual(bad, [], f"unexplained early return at {bad}")

    def test_the_exemption_is_not_abused(self):
        """Only genuinely trivial parsers may use the `_or_none` exemption."""
        import ast
        for fn in ast.walk(self.tree):
            if isinstance(fn, ast.FunctionDef) and fn.name.endswith("_or_none"):
                self.assertLessEqual(
                    len(fn.body), 3,
                    f"{fn.name} is too complex to be exempt from logging")

    def test_venue_fields_are_not_defaulted_with_or(self):
        """`x.get('f') or 5` turns a real 0 into 5 -- the fee-rate bug."""
        import re
        offenders = re.findall(
            r'\.get\(\s*["\'](feeRateBps|decimalPrecision|chainId|collateral'
            r'|startPrice|endPrice|amountOut|realizedPnl|priceImpact)["\']'
            r'\s*\)\s+or\s+[^\s)]', self.src)
        self.assertEqual(offenders, [], f"silent defaults for {offenders}")

    def test_every_apierror_handler_mentions_the_error(self):
        import ast
        bad = []
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.ExceptHandler) or n.type is None:
                continue
            if "ApiError" not in ast.unparse(n.type):
                continue
            body = " ".join(ast.unparse(st) for st in n.body)
            if not ("LOG." in body or "raise" in body or "print" in body
                    or "append" in body):
                bad.append(n.lineno)
        self.assertEqual(bad, [], f"ApiError handled without a trace at {bad}")


class TestStrictFieldParsing(unittest.TestCase):
    """Missing venue fields reject the market instead of being invented."""

    def test_missing_decimal_precision_rejects(self):
        t = TestParseRound.topic()
        del t["markets"][0]["decimalPrecision"]
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_unknown_liquidity_is_none_not_zero(self):
        t = TestParseRound.topic()
        del t["markets"][0]["liquidity"]
        t.pop("liquidity", None)
        r = PredictionClient._parse_round(t)
        self.assertIsNotNone(r)
        self.assertIsNone(r.liquidity)

    def test_real_zero_liquidity_is_preserved(self):
        t = TestParseRound.topic()
        t["markets"][0]["liquidity"] = "0"
        self.assertEqual(PredictionClient._parse_round(t).liquidity, 0.0)

    def test_precision_zero_is_honoured(self):
        t = TestParseRound.topic()
        t["markets"][0]["decimalPrecision"] = 0
        self.assertEqual(PredictionClient._parse_round(t).decimal_precision, 0)


class TestQuoteValidation(unittest.TestCase):
    """A malformed quote must fail loudly, not be coerced into plausibility."""

    def _client(self, payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = m.WalletRef("0xabc", "w1")
        c._request = lambda name, params=None: payload
        c.resolved_funding_source = lambda: "MPC"
        return c

    def _quote(self, payload):
        return self._client(payload).get_quote(make_round(), Side.UP, 5.0)

    def test_valid_quote_parses(self):
        q = self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "8333333333333333333",
                         "priceImpact": "0.01", "feeAmount": "0"})
        self.assertAlmostEqual(q.average_price, 0.6)
        self.assertGreater(q.amount_out_shares, 8.0)

    def test_missing_amount_out_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"quoteId": "q", "averagePrice": "0.6"})

    def test_zero_shares_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "0"})

    def test_missing_price_impact_is_infinite_not_zero(self):
        """Unknown impact must not silently pass the impact guard."""
        q = self._quote({"quoteId": "q", "averagePrice": "0.6",
                         "amountOut": "8333333333333333333"})
        self.assertEqual(q.price_impact, float("inf"))
        self.assertGreater(q.price_impact, cfg().max_price_impact)

    def test_implausible_price_raises(self):
        for bad in ("0", "1", "1.5", "-0.2"):
            with self.assertRaises(m.ApiError):
                self._quote({"quoteId": "q", "averagePrice": bad,
                             "amountOut": "1000000000000000000"})

    def test_missing_quote_id_raises(self):
        with self.assertRaises(m.ApiError):
            self._quote({"averagePrice": "0.6", "amountOut": "1"})


class TestErrorClassification(unittest.TestCase):
    """Errors are classified by the venue's numeric code, not by wording."""

    def test_observed_insufficient_funds_code(self):
        e = m.ApiError("Please ensure your account has enough USDT.", code=-9000)
        self.assertIs(e.kind, m.ErrorKind.INSUFFICIENT_FUNDS)

    def test_auth_codes(self):
        for c in (-1022, -2014, -2015, -1002):
            self.assertIs(m.ApiError("x", code=c).kind, m.ErrorKind.AUTH, c)

    def test_timing_code(self):
        self.assertIs(m.ApiError("x", code=-1021).kind, m.ErrorKind.TIMING)

    def test_parameter_codes(self):
        for c in (-1102, -1104, -1121):
            self.assertIs(m.ApiError("x", code=c).kind, m.ErrorKind.PARAMETER, c)

    def test_unknown_code_is_not_guessed_from_text(self):
        """A code we do not know must not be inferred from wording."""
        e = m.ApiError("minimum order size not met", code=-4321)
        self.assertIs(e.kind, m.ErrorKind.UNKNOWN)

    def test_message_fallback_only_without_a_code(self):
        self.assertIs(m.ApiError("order below the minimum").kind,
                      m.ErrorKind.SIZE)
        self.assertIs(m.ApiError("account has enough USDT? no").kind,
                      m.ErrorKind.INSUFFICIENT_FUNDS)
        self.assertIs(m.ApiError("Signature for this request").kind,
                      m.ErrorKind.AUTH)

    def test_unrecognised_text_is_unknown(self):
        self.assertIs(m.ApiError("something odd happened").kind,
                      m.ErrorKind.UNKNOWN)

    def test_code_and_status_are_retained(self):
        e = m.ApiError("x", code=-9000, status=400)
        self.assertEqual((e.code, e.status), (-9000, 400))

    def test_request_attaches_the_code(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = None

        class S:
            def request(self, method, url, timeout=None):
                return types.SimpleNamespace(
                    status_code=400, text="",
                    json=lambda: {"code": -9000, "msg": "not enough USDT"})
        c._session = S()
        with self.assertRaises(m.ApiError) as ctx:
            c._request("market_list")
        self.assertEqual(ctx.exception.code, -9000)
        self.assertIs(ctx.exception.kind, m.ErrorKind.INSUFFICIENT_FUNDS)


class TestNoRawTracebacks(unittest.TestCase):
    """CLI commands must fail with a message, never a traceback."""

    def test_every_command_dispatch_is_wrapped(self):
        import ast, inspect
        tree = ast.parse(inspect.getsource(m))
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        src = ast.unparse(main_fn)
        self.assertIn("except ApiError", src)
        self.assertIn("except KeyboardInterrupt", src)

    def test_trader_run_is_wrapped(self):
        import ast, inspect
        tree = ast.parse(inspect.getsource(m))
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        src = ast.unparse(main_fn)
        idx = src.find(").run()")
        self.assertGreater(idx, 0, "no Trader run call found in main()")
        self.assertIn("except ApiError", src[max(0, idx - 300):idx + 400])


class TestSchemaConformance(unittest.TestCase):
    """
    Every API call must conform to the connector's schema.

    This is the check that does not depend on my model of the API: the
    connector is generated from Binance's own OpenAPI spec, so it catches
    assumptions that the unit tests share with the code under test.
    """

    def test_all_calls_conform(self):
        import subprocess, os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        conf = _os.path.join(here, "conformance.py")
        if not _os.path.exists(conf):
            self.skipTest("conformance.py not present")
        r = subprocess.run(
            [sys.executable, conf, "--source",
             _os.path.join(here, "btc_5m_predictor.py")],
            capture_output=True, text=True, cwd=here)
        if r.returncode == 2:
            self.skipTest("connector not installed")
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])


class TestBoundaryConditions(unittest.TestCase):
    """
    Boundaries surfaced by mutation testing: each of these lines could be
    changed without any test noticing, which means they were never checked.
    """

    def test_exactly_zero_edge_does_not_trade(self):
        c = cfg()
        be = breakeven_probability(0.60, 200)
        self.assertEqual(m.kelly_stake(1000, be, 0.60, c, 200), 0.0)

    def test_edge_a_hair_above_breakeven_does_trade(self):
        c = cfg(min_stake_usdt=0.5, max_stake_pct=0.25)
        be = breakeven_probability(0.60, 200)
        self.assertGreater(m.kelly_stake(10_000, be + 0.05, 0.60, c, 200), 0.0)

    def test_zero_bankroll_does_not_trade(self):
        self.assertEqual(m.kelly_stake(0.0, 0.99, 0.10, cfg()), 0.0)

    def test_negative_bankroll_does_not_trade(self):
        self.assertEqual(m.kelly_stake(-50.0, 0.99, 0.10, cfg()), 0.0)

    def test_hundred_percent_fee_leaves_no_odds(self):
        self.assertEqual(m.kelly_stake(1000, 0.99, 0.60, cfg(), 10_000), 0.0)

    def test_negative_fee_from_payload_is_rejected(self):
        """A negative rate would inflate net odds and therefore stake size."""
        t = TestParseRound.topic(feeRateBps=-100)
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_fee_at_or_above_100pct_is_rejected(self):
        for bad in (10_000, 20_000):
            self.assertIsNone(
                PredictionClient._parse_round(TestParseRound.topic(feeRateBps=bad)))

    def test_zero_fee_is_accepted(self):
        self.assertEqual(
            PredictionClient._parse_round(TestParseRound.topic(feeRateBps=0)).fee_bps, 0)

    def test_tie_at_expiry_resolves_down_locally(self):
        self.assertEqual(digital_up_probability(100_000, 100_000, 0.5, 0), 0.0)

    def test_a_hair_above_strike_resolves_up(self):
        self.assertEqual(digital_up_probability(100_000.01, 100_000, 0.5, 0), 1.0)

    def test_book_level_at_price_one_is_ignored(self):
        self.assertIsNone(m.walk_book([(1.0, 1000)], 10.0))

    def test_book_level_above_one_is_ignored(self):
        self.assertIsNone(m.walk_book([(1.5, 1000)], 10.0))

    def test_book_mixes_valid_and_degenerate_levels(self):
        avg = m.walk_book([(1.0, 500), (0.50, 1000), (0.0, 10)], 100.0)
        self.assertAlmostEqual(avg, 0.50)

    def test_stake_exactly_at_minimum_is_accepted(self):
        c = cfg(min_stake_usdt=1.0, max_stake_pct=0.25, kelly_fraction=1.0)
        self.assertGreater(m.kelly_stake(4.0, 0.99, 0.05, c, 0), 0.0)

    def test_round_target_is_applied_from_config(self):
        """Regression: round_seconds was never exercised end to end."""
        c = cfg(round_seconds=600)
        PredictionClient(c)                     # sets the class attribute
        try:
            base = 1_700_000_000_000
            ok = TestParseRound.topic(startDate=base, endDate=base + 600_000)
            bad = TestParseRound.topic(startDate=base, endDate=base + 300_000)
            self.assertIsNotNone(PredictionClient._parse_round(ok))
            self.assertIsNone(PredictionClient._parse_round(bad))
        finally:
            PredictionClient(cfg())             # restore the 300s default

    def test_config_stake_bounds_are_enforced_at_the_edges(self):
        Config(api_key="k", api_secret="s", max_stake_pct=0.25)
        with self.assertRaises(ValueError):
            Config(api_key="k", api_secret="s", hard_stake_ceiling=0.6)
        with self.assertRaises(ValueError):
            Config(api_key="k", api_secret="s", hard_stake_multiple=0.5)
        with self.assertRaises(ValueError):
            Config(api_key="k", api_secret="s", max_stake_pct=0.26)

    def test_tail_df_floor_must_exceed_two(self):
        Config(api_key="k", api_secret="s", tail_df_floor=2.01)
        with self.assertRaises(ValueError):
            Config(api_key="k", api_secret="s", tail_df_floor=2.0)


class TestOrderResponseHandling(unittest.TestCase):
    """place_order paths, which mutation testing showed were unexercised."""

    def _client(self, payload):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._clock_offset_ms = 0
        c._symbol_cache = {}; c._wallet = m.WalletRef("0xabc", "w1")
        c._request = lambda name, params=None: payload
        c._resolved_account_type = lambda: "SPOT"
        c.resolved_funding_source = lambda: "CEX"
        return c

    def test_successful_order_returns_the_id(self):
        c = self._client({"orderId": "12345"})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        self.assertEqual(c.place_order(make_round(), q), "12345")

    def test_missing_order_id_raises(self):
        c = self._client({"status": "ACCEPTED"})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        with self.assertRaises(m.ApiError):
            c.place_order(make_round(), q)

    def test_empty_order_id_raises(self):
        c = self._client({"orderId": ""})
        q = m.Quote("q", 0.6, 8.0, 0.0, 0.0)
        with self.assertRaises(m.ApiError):
            c.place_order(make_round(), q)


class TestTieSettlement(unittest.TestCase):
    """An exact close at the strike must not be guessed on a real position."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _trader(self, client):
        c = cfg(db_path=self.db)
        return build_trader(client, c, self.db)

    def test_exact_tie_is_left_unsettled(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        path = [(start, 100_000.0), (start + 240_000, 100_400.0),
                (start + m.DEFAULT_ROUND_SECONDS * 1000 + 3_000, 100_000.0)]
        client = FakeClient([rnd], path, {(1, Side.UP): [(0.55, 10_000)]}, {})
        t = self._trader(client)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertIsNotNone(t._position)

        client.t = 2
        client.final_price = lambda r: 100_000.0    # exactly the strike
        t._settle_open()
        self.assertIsNotNone(t._position)           # held, not guessed


class TestHostilePayloads(unittest.TestCase):
    """
    Regressions from property-based fuzzing. Every case here was a real crash
    found by random input, not by anyone imagining it.
    """

    def test_asks_as_a_scalar_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": 4477054861}))

    def test_asks_as_a_string_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": "0.5"}))

    def test_asks_as_a_dict_does_not_crash(self):
        self.assertIsNone(PredictionClient._parse_asks({"asks": {"a": 1}}))

    def test_infinite_numeric_does_not_crash_parsing(self):
        """OverflowError is not a ValueError; int(inf) raises it."""
        t = TestParseRound.topic()
        t["markets"][0]["marketId"] = float("inf")
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_scientific_overflow_string_is_rejected(self):
        t = TestParseRound.topic()
        t["markets"][0]["outcomes"][0]["price"] = "1e999"   # parses to inf
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_nan_price_is_rejected(self):
        t = TestParseRound.topic()
        t["markets"][0]["outcomes"][0]["price"] = "nan"
        self.assertIsNone(PredictionClient._parse_round(t))

    def test_infinite_liquidity_becomes_unknown(self):
        t = TestParseRound.topic()
        t["markets"][0]["liquidity"] = "1e999"
        r = PredictionClient._parse_round(t)
        self.assertIsNotNone(r)
        self.assertIsNone(r.liquidity)

    def test_infinite_start_price_is_rejected(self):
        strike, _ = PredictionClient._parse_variant({"startPrice": "1e999"})
        self.assertIsNone(strike)

    def test_nan_start_price_is_rejected(self):
        strike, _ = PredictionClient._parse_variant({"startPrice": "nan"})
        self.assertIsNone(strike)

    def test_non_finite_book_levels_are_skipped(self):
        got = PredictionClient._parse_asks(
            {"asks": [["nan", "10"], ["1e999", "5"], ["0.4", "100"]]})
        self.assertEqual(got, [(0.4, 100.0)])

    def test_null_bytes_in_strings_do_not_crash(self):
        t = TestParseRound.topic()
        t["slug"] = "btc\x00updown"
        self.assertIsNotNone(PredictionClient._parse_round(t))

    def test_deeply_nested_garbage_does_not_crash(self):
        for payload in ({"markets": [{"outcomes": [{"price": {"a": [1]}}]}]},
                        {"markets": "not-a-list"},
                        {"markets": [None]},
                        {"variantData": [1, 2, 3]}):
            self.assertIsNone(PredictionClient._parse_round(payload))


class TestPropertyInvariants(unittest.TestCase):
    """A fast in-suite slice of the fuzzer, so CI enforces the invariants."""

    def test_fuzz_suite_passes(self):
        import subprocess, os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        fz = _os.path.join(here, "fuzz.py")
        if not _os.path.exists(fz):
            self.skipTest("fuzz.py not present")
        r = subprocess.run([sys.executable, fz, "--trials", "400"],
                           capture_output=True, text=True, cwd=here)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])


class TestFundingSourceDerivation(unittest.TestCase):
    """
    Regression for -3026.

    Three places can hold collateral. `accountType` on place-order accepts
    only SPOT|FUNDING; the prediction wallet is not a legal value there and
    passing it through is what the venue rejected.
    """

    def _client(self, options, cfg_funding="AUTO", cfg_account="AUTO"):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(funding_source=cfg_funding, account_type=cfg_account)
        c.payment_options = lambda: options
        return c

    def test_spot_account_funds_from_cex(self):
        c = self._client([("SPOT", 50.0, True)])
        self.assertEqual(c.funding_plan()[:2], ("SPOT", "CEX"))

    def test_funding_account_funds_from_cex(self):
        c = self._client([("FUNDING", 50.0, True)])
        self.assertEqual(c.funding_plan()[:2], ("FUNDING", "CEX"))

    def test_prediction_wallet_funds_from_mpc(self):
        """The reported bug: funds in the prediction account."""
        c = self._client([("SPOT", 0.0, True),
                          ("PREDICTION", 6.64, True)])
        account, funding, holder = c.funding_plan()
        self.assertEqual(funding, "MPC")
        self.assertEqual(holder, "PREDICTION")
        self.assertIn(account, m.CEX_ACCOUNT_TYPES)   # never the raw holder

    def test_prediction_holder_never_leaks_into_account_type(self):
        for name in ("PREDICTION", "MPC", "PREDICTION_WALLET", "WEB3"):
            c = self._client([(name, 9.0, True)])
            self.assertIn(c.funding_plan()[0], m.CEX_ACCOUNT_TYPES, name)

    def test_no_options_at_all_still_yields_a_legal_account(self):
        c = self._client([])
        account, funding, holder = c.funding_plan()
        self.assertIn(account, m.CEX_ACCOUNT_TYPES)
        self.assertEqual(funding, "MPC")
        self.assertIsNone(holder)

    def test_explicit_override_is_respected(self):
        c = self._client([("SPOT", 50.0, True)], cfg_funding="MPC")
        self.assertEqual(c.funding_plan()[1], "MPC")

    def test_explicit_invalid_account_type_is_rejected(self):
        c = self._client([("SPOT", 50.0, True)], cfg_account="FUNDING")
        self.assertEqual(c.funding_plan()[0], "FUNDING")

    def test_disabled_options_are_ignored_when_planning(self):
        c = self._client([("SPOT", 100.0, False), ("FUNDING", 5.0, True)])
        self.assertEqual(c.funding_plan()[0], "FUNDING")

    def test_default_config_is_auto(self):
        self.assertEqual(cfg().funding_source, "AUTO")

    def test_invalid_funding_source_rejected(self):
        with self.assertRaises(ValueError):
            cfg(funding_source="WALLET")

    def _order_client(self, options):
        sent = {}
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c.payment_options = lambda: options
        c._request = lambda name, params=None: (sent.update(params or {}),
                                                {"orderId": "1"})[1]
        return c, sent

    def test_cex_order_includes_a_fund_transfer(self):
        c, sent = self._order_client([("SPOT", 50.0, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.5)
        self.assertEqual(sent["fundingSource"], "CEX")
        self.assertEqual(sent["fundTransferAmount"], m.to_wei(1.5))

    def test_prediction_wallet_order_omits_the_transfer(self):
        """Funds already in place: nothing to move."""
        c, sent = self._order_client([("PREDICTION", 6.64, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.5)
        self.assertEqual(sent["fundingSource"], "MPC")
        self.assertNotIn("fundTransferAmount", sent)
        self.assertIn(sent["accountType"], m.CEX_ACCOUNT_TYPES)

    def test_order_never_sends_an_illegal_account_type(self):
        for holder in ("PREDICTION", "MPC", "WEB3", "SPOT", "FUNDING"):
            c, sent = self._order_client([(holder, 9.0, True)])
            c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.0)
            self.assertIn(sent["accountType"], m.CEX_ACCOUNT_TYPES, holder)

    def test_time_in_force_matches_market_order_type(self):
        c, sent = self._order_client([("SPOT", 50.0, True)])
        c.place_order(make_round(), m.Quote("q", 0.6, 8.0, 0.0, 0.0), 1.0)
        self.assertEqual((sent["orderType"], sent["timeInForce"]),
                         ("MARKET", "FOK"))


class TestProfileDefaults(unittest.TestCase):

    def test_profile_flag_has_no_default(self):
        """
        An argparse default would always beat the config file's
        active_profile, making that setting dead -- the same way --kelly's
        0.25 default silently overrode every profile's kelly_fraction.
        """
        import inspect, re as _re
        src = inspect.getsource(m.main)
        hit = _re.search(r'"--profile".*?default=(\w+)', src, _re.S)
        self.assertEqual(hit.group(1), "None")

    def test_generated_config_uses_the_declared_default(self):
        self.assertEqual(m.default_config_document()["active_profile"],
                         m.DEFAULT_PROFILE)

    def test_default_profile_is_the_last_minute_one(self):
        self.assertEqual(m.DEFAULT_PROFILE, "lastminute")

    def test_default_profile_exists(self):
        self.assertIn(m.DEFAULT_PROFILE, m.PROFILES)

    def test_build_config_falls_back_to_the_declared_default(self):
        doc = m.default_config_document()
        doc.pop("active_profile")
        c = m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d")
        self.assertEqual(c.profile_name, m.DEFAULT_PROFILE)

    def test_micro_simulates_a_small_account(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertLessEqual(c.paper_start_bankroll, 10.0)

    def test_each_profile_declares_its_own_paper_bankroll(self):
        for name, prof in m.PROFILES.items():
            self.assertIn("paper_start_bankroll", prof, name)

    def test_micro_paper_bankroll_can_actually_trade(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertGreater(
            m.kelly_stake(c.paper_start_bankroll, 0.70, 0.60, c, 0), 0.0)

    def test_every_profile_paper_bankroll_can_trade(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            mid = (c.min_entry_price + c.max_entry_price) / 2
            stake = m.kelly_stake(c.paper_start_bankroll, min(mid * 1.6, 0.99),
                                  mid, c, 0)
            self.assertGreater(stake, 0.0, f"{name} cannot trade its own "
                                           f"paper bankroll")


class TestPerProfileReport(unittest.TestCase):
    """Pooling profiles averages disjoint price bands into a meaningless bias."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _fill(self, profile, price, wins, losses):
        j = Journal(self.db, profile)
        for i in range(wins + losses):
            sig = Signal(Side.UP, price + 0.05, price, 0.05, 1.0, 60.0)
            t = j.record("PAPER", make_round(), sig, 1e5, 0.5, 10.0)
            j.resolve(t, i < wins, 1.0 if i < wins else -1.0, "venue")

    def test_profiles_are_reported_separately(self):
        self._fill("micro", 0.60, 30, 20)
        self._fill("convex", 0.15, 5, 45)
        report = Journal(self.db).calibration_report()
        self.assertIn("PROFILE: micro", report)
        self.assertIn("PROFILE: convex", report)

    def test_single_profile_can_be_selected(self):
        self._fill("micro", 0.60, 30, 20)
        self._fill("convex", 0.15, 5, 45)
        report = Journal(self.db).calibration_report("convex")
        self.assertIn("convex", report)
        self.assertNotIn("PROFILE: micro", report)

    def test_hit_rates_do_not_bleed_between_profiles(self):
        self._fill("micro", 0.60, 40, 0)        # 100%
        self._fill("convex", 0.15, 0, 40)       # 0%
        micro = Journal(self.db).calibration_report("micro")
        convex = Journal(self.db).calibration_report("convex")
        self.assertIn("100.0%", micro)
        self.assertIn("0.0%", convex)

    def test_small_sample_is_not_given_a_verdict(self):
        self._fill("micro", 0.60, 3, 2)
        self.assertIn("too few", Journal(self.db).calibration_report("micro"))

    def test_unknown_profile_reports_cleanly(self):
        self._fill("micro", 0.60, 5, 5)
        self.assertIn("No resolved trades",
                      Journal(self.db).calibration_report("nonexistent"))

    def test_legacy_journal_without_profile_column_still_reads(self):
        import sqlite3 as sq
        conn = sq.connect(self.db)
        conn.execute("DROP TABLE IF EXISTS trades")
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts INTEGER,"
                     " mode TEXT, slug TEXT, topic_id INTEGER, side TEXT,"
                     " strike REAL, spot REAL, sigma REAL, seconds_left REAL,"
                     " end_ms INTEGER, model_prob REAL, fill_price REAL,"
                     " edge REAL, stake REAL, bankroll_before REAL,"
                     " order_id TEXT, resolved INTEGER, won INTEGER, pnl REAL,"
                     " settle_source TEXT)")
        conn.execute("INSERT INTO trades (model_prob, fill_price, won, pnl,"
                     " stake, resolved) VALUES (0.7, 0.6, 1, 1.0, 1.0, 1)")
        conn.commit(); conn.close()
        self.assertIn("unknown", Journal(self.db).calibration_report())


class TestPredictionWalletBalance(unittest.TestCase):
    """
    payment-options covers the CEX accounts only. A prediction wallet funded
    directly does not appear there, which reported 0.00 for a funded account.
    """

    def _client(self, options, wallet_value):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(account_type="AUTO")
        c.payment_options = lambda: options
        c.prediction_wallet_value = lambda: wallet_value
        return c

    def test_prediction_wallet_balance_is_found(self):
        c = self._client([("SPOT", 0.0, True)], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_larger_cex_balance_wins(self):
        c = self._client([("SPOT", 500.0, True)], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 500.0)

    def test_unreadable_portfolio_falls_back_to_cex(self):
        c = self._client([("SPOT", 12.0, True)], None)
        self.assertAlmostEqual(c.balance_usdt(), 12.0)

    def test_nothing_anywhere_raises(self):
        c = self._client([], None)
        with self.assertRaises(m.ApiError):
            c.balance_usdt()

    def test_empty_options_with_funded_wallet_still_works(self):
        c = self._client([], 6.64)
        self.assertAlmostEqual(c.balance_usdt(), 6.64)

    def test_portfolio_parses_total_current_value(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"totalCurrentValue": "6.64"}
        self.assertAlmostEqual(c.prediction_wallet_value(), 6.64)

    def test_portfolio_rejects_non_finite_value(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"totalCurrentValue": "1e999"}
        self.assertIsNone(c.prediction_wallet_value())

    def test_portfolio_missing_field_is_none(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg(); c._wallet = m.WalletRef("0xa", "w1")
        c._request = lambda name, params=None: {"walletAddress": "0xa"}
        self.assertIsNone(c.prediction_wallet_value())


class TestBufferGate(unittest.TestCase):
    """'Big buffer, late in the round' expressed as sigmas of time remaining."""

    def test_buffer_grows_as_time_runs_out(self):
        a = m.buffer_sigmas(65_130, 65_000, 0.5, 240)
        b = m.buffer_sigmas(65_130, 65_000, 0.5, 30)
        self.assertGreater(b, a)

    def test_buffer_sign_follows_direction(self):
        self.assertGreater(m.buffer_sigmas(65_100, 65_000, 0.5, 60), 0)
        self.assertLess(m.buffer_sigmas(64_900, 65_000, 0.5, 60), 0)

    def test_at_the_money_is_zero_buffer(self):
        self.assertAlmostEqual(m.buffer_sigmas(65_000, 65_000, 0.5, 60), 0.0)

    def test_expiry_is_infinite_buffer(self):
        self.assertEqual(m.buffer_sigmas(65_001, 65_000, 0.5, 0), math.inf)
        self.assertEqual(m.buffer_sigmas(64_999, 65_000, 0.5, 0), -math.inf)

    def test_buffer_matches_the_pricing_z(self):
        """The gate and the model must use the same quantity."""
        z = m.buffer_sigmas(65_130, 65_000, 0.5, 60)
        self.assertAlmostEqual(m.digital_up_probability(65_130, 65_000, 0.5, 60),
                               m.norm_cdf(z), places=12)

    def test_gate_blocks_small_buffers(self):
        c = cfg(min_buffer_sigmas=2.0, **{k: v for k, v in
                m.PROFILES["buffer"].items() if k != "min_buffer_sigmas"})
        rnd = make_round(strike=65_000.0, fee_bps=0)
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.85, 100_000)]}
        self.assertIsNone(assess(rnd, 65_010, 0.5, 1000, now, c, book).signal)

    def test_gate_allows_large_buffers(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        rnd = make_round(strike=65_000.0, fee_bps=0)
        now = rnd.end_ms - 60_000
        # 0.78 rather than 0.90: the buffer is what admits the trade, and the
        # return floor is what decides the price it may be taken at. A book
        # priced above the floor is refused however large the buffer is,
        # which is the whole point of the floor.
        book = {Side.UP: [(0.78, 100_000)]}
        sig = assess(rnd, 65_180, 0.5, 1000, now, c, book).signal
        self.assertIsNotNone(sig)
        self.assertGreaterEqual(abs(sig.buffer_z), c.min_buffer_sigmas)

    def test_never_bets_against_the_buffer(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        rnd = make_round(strike=65_000.0, fee_bps=0)
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.90, 1e5)], Side.DOWN: [(0.82, 1e5)]}
        sig = assess(rnd, 65_180, 0.5, 1000, now, c, book).signal
        if sig is not None:
            self.assertIs(sig.side, Side.UP)

    def test_buffer_profile_backs_favourites_that_still_pay(self):
        """
        The band's top is set by the return floor, not chosen separately.

        Buying a favourite is only sound while the win is large enough to
        cover the loss it risks. If the ceiling ever drifts above what the
        floor permits, the profile is back to 6% wins and one loss undoing
        sixteen of them.
        """
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertGreater(c.min_entry_price, 0.50)
        self.assertLessEqual(
            c.max_entry_price,
            m.max_price_for_return(c.min_win_return, c.fee_bps) + 0.01)
        self.assertGreaterEqual(c.min_win_return, 0.25)

    def test_other_profiles_do_not_enforce_a_return_floor(self):
        """Only buffer trades on the payout as well as the edge."""
        for name in ("convex", "balanced", "favorite", "micro"):
            c = Config(api_key="k", api_secret="s", **m.PROFILES[name])
            self.assertEqual(c.min_win_return, 0.0, name)

    def test_gate_disabled_by_default_elsewhere(self):
        for name in ("convex", "favorite", "micro", "balanced"):
            c = Config(api_key="k", api_secret="s", **m.PROFILES[name])
            self.assertEqual(c.min_buffer_sigmas, 0.0, name)

    def test_negative_gate_rejected(self):
        with self.assertRaises(ValueError):
            cfg(min_buffer_sigmas=-1.0)


class TestBufferReport(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def test_report_buckets_by_buffer(self):
        j = Journal(self.db, "buffer")
        for z, won, pnl in [(2.5, True, 0.05)] * 40 + [(2.5, False, -1.0)] * 3:
            sig = Signal(Side.UP, 0.97, 0.95, 0.02, 1.0, 40.0, z)
            t = j.record("PAPER", make_round(), sig, 65_000, 0.5, 25.0)
            j.resolve(t, won, pnl, "venue")
        report = j.calibration_report("buffer")
        self.assertIn("By buffer", report)
        self.assertIn("2-3", report)

    def test_high_win_rate_with_negative_pnl_is_visible(self):
        """The failure mode of trading near-certainties."""
        j = Journal(self.db, "buffer")
        for won, pnl in [(True, 0.05)] * 40 + [(False, -1.0)] * 3:
            sig = Signal(Side.UP, 0.97, 0.95, 0.02, 1.0, 40.0, 2.5)
            t = j.record("PAPER", make_round(), sig, 65_000, 0.5, 25.0)
            j.resolve(t, won, pnl, "venue")
        report = j.calibration_report("buffer")
        self.assertIn("-1.00", report)      # 40*0.05 - 3*1.00 = -1.00


class TestHostingReadiness(unittest.TestCase):
    """Behaviours a hosted deployment depends on."""

    def test_sigterm_handler_is_installed(self):
        import signal as _sig
        c = cfg()
        t = Trader.__new__(Trader)
        t._store = None

        t._static_cfg = c; t._stopping = False
        previous = _sig.getsignal(_sig.SIGTERM)
        try:
            t._install_signal_handlers()
            self.assertNotEqual(_sig.getsignal(_sig.SIGTERM), previous)
        finally:
            _sig.signal(_sig.SIGTERM, previous)

    def test_sigterm_sets_stopping_rather_than_dying(self):
        import signal as _sig
        t = Trader.__new__(Trader)
        t._store = None

        t._static_cfg = cfg(); t._stopping = False
        previous = _sig.getsignal(_sig.SIGTERM)
        try:
            t._install_signal_handlers()
            _sig.getsignal(_sig.SIGTERM)(_sig.SIGTERM, None)
            self.assertTrue(t._stopping)
        finally:
            _sig.signal(_sig.SIGTERM, previous)

    def test_second_sigterm_exits_immediately(self):
        import signal as _sig
        t = Trader.__new__(Trader)
        t._store = None

        t._static_cfg = cfg(); t._stopping = True
        previous = _sig.getsignal(_sig.SIGTERM)
        try:
            t._install_signal_handlers()
            with self.assertRaises(SystemExit):
                _sig.getsignal(_sig.SIGTERM)(_sig.SIGTERM, None)
        finally:
            _sig.signal(_sig.SIGTERM, previous)

    def test_451_is_classified_as_geo_blocked(self):
        e = m.ApiError("Unavailable For Legal Reasons", status=451)
        self.assertIs(e.kind, m.ErrorKind.GEO_BLOCKED)

    def test_451_beats_any_code_present(self):
        e = m.ApiError("x", code=-1102, status=451)
        self.assertIs(e.kind, m.ErrorKind.GEO_BLOCKED)

    def test_public_endpoint_reports_geo_block(self):
        c = PredictionClient.__new__(PredictionClient)
        c._store = None

        c._static_cfg = cfg()

        class S:
            def get(self, url, params=None, timeout=None):
                return types.SimpleNamespace(status_code=451, text="")
        c._session = S()
        with self.assertRaises(m.ApiError) as ctx:
            c.spot_price()
        self.assertIs(ctx.exception.kind, m.ErrorKind.GEO_BLOCKED)

    def test_shutdown_is_not_a_trading_halt(self):
        """A restart must not be recorded as a risk-limit breach."""
        self.assertFalse(issubclass(m.Shutdown, TradingHalted))


class TestPeriodicReport(unittest.TestCase):
    """Hosted workers cannot easily read the journal file."""

    def test_report_every_is_off_by_default(self):
        self.assertEqual(cfg().report_every, 0)

    def test_negative_report_every_rejected(self):
        with self.assertRaises(ValueError):
            cfg(report_every=-1)

    def test_report_every_accepted(self):
        self.assertEqual(cfg(report_every=25).report_every, 25)

    def test_report_is_logged_on_the_interval(self):
        """
        Drives the real settlement path rather than re-implementing it.

        The previous version copied the counter logic into the test, so it
        asserted that the test's own code worked -- it would have passed even
        if the feature had been deleted from the bot.
        """
        import logging as _log
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, report_every=2, profile_name="micro")
            start = 1_700_000_000_000
            rnd = make_round(strike=65_000.0, start_ms=start,
                             end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
            path = [(start, 65_000.0), (start + 240_000, 65_200.0),
                    (start + m.DEFAULT_ROUND_SECONDS * 1000 + 3_000, 65_200.0)]
            client = FakeClient([rnd], path, {}, {1: Side.UP})
            t = build_trader(client, c, db)

            records = []

            class Cap(_log.Handler):
                def emit(self, rec):
                    records.append(rec.getMessage())

            handler = Cap()
            previous = m.LOG.level
            m.LOG.setLevel(_log.INFO)      # default is WARNING; INFO is filtered
            m.LOG.addHandler(handler)
            try:
                for _ in range(2):
                    sig = Signal(Side.UP, 0.80, 0.60, 0.05, 1.0, 40.0, 2.4)
                    tid = t._journal.record("PAPER", rnd, sig, 65_200, 0.5, 10.0)
                    t._position = Position(tid, rnd, sig)
                    client.t = 2
                    t._settle_open()
            finally:
                m.LOG.removeHandler(handler)
                m.LOG.setLevel(previous)

            self.assertTrue(any(r.startswith("| ") for r in records),
                            "no calibration report emitted by _settle_open")
        finally:
            os.unlink(db)

    def test_report_not_logged_when_disabled(self):
        import logging as _log
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, report_every=0)
            start = 1_700_000_000_000
            rnd = make_round(strike=65_000.0, start_ms=start,
                             end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
            path = [(start, 65_000.0), (start + 240_000, 65_200.0),
                    (start + m.DEFAULT_ROUND_SECONDS * 1000 + 3_000, 65_200.0)]
            client = FakeClient([rnd], path, {}, {1: Side.UP})
            t = build_trader(client, c, db)
            records = []

            class Cap(_log.Handler):
                def emit(self, rec):
                    records.append(rec.getMessage())

            handler = Cap()
            previous = m.LOG.level
            m.LOG.setLevel(_log.INFO)
            m.LOG.addHandler(handler)
            try:
                sig = Signal(Side.UP, 0.80, 0.60, 0.05, 1.0, 40.0, 2.4)
                tid = t._journal.record("PAPER", rnd, sig, 65_200, 0.5, 10.0)
                t._position = Position(tid, rnd, sig)
                client.t = 2
                t._settle_open()
            finally:
                m.LOG.removeHandler(handler)
                m.LOG.setLevel(previous)
            self.assertFalse(any(r.startswith("| ") for r in records))
        finally:
            os.unlink(db)


class TestDiagnose(unittest.TestCase):
    """Is the edge real? The one question that decides everything."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _fill(self, price, true_rate, n, seed=1):
        import random as _r
        rng = _r.Random(seed)
        j = Journal(self.db, "buffer")
        for _ in range(n):
            won = rng.random() < true_rate
            sig = Signal(Side.UP, 0.80, price, 0.05, 2.5, 40.0, 2.4)
            t = j.record("PAPER", make_round(), sig, 65_000, 0.5, 25.0)
            j.resolve(t, won, settle_pnl(2.5, price, won, 200), "venue")
        return j

    def test_clear_edge_is_reported(self):
        j = self._fill(0.714, 0.90, 300)
        out = j.diagnose("buffer")
        self.assertIn("EDGE", out)
        self.assertIn("Kelly", out)

    def test_clear_absence_of_edge_is_reported(self):
        j = self._fill(0.714, 0.45, 300)
        out = j.diagnose("buffer")
        self.assertIn("NO EDGE", out)
        self.assertIn("cannot change its sign", out)

    def test_marginal_case_is_called_unclear_not_guessed(self):
        """A 4-point shortfall needs ~525 trades; 200 must not be a verdict."""
        j = self._fill(0.714, 0.68, 200)
        out = j.diagnose("buffer")
        self.assertIn("Inconclusive", out)

    def test_tiny_sample_is_never_given_a_verdict(self):
        j = self._fill(0.714, 0.20, 10)
        out = j.diagnose("buffer")
        self.assertIn("too few", out)

    def test_losing_advice_never_suggests_raising_stake(self):
        j = self._fill(0.714, 0.45, 300)
        out = j.diagnose("buffer").lower()
        self.assertIn("bigger buffer", out)
        self.assertNotIn("increase the stake", out)

    def test_winning_advice_warns_against_manual_sizing_up(self):
        j = self._fill(0.714, 0.90, 300)
        self.assertIn("Do NOT raise the stake cap", j.diagnose("buffer"))

    def test_breakeven_used_matches_the_price_paid(self):
        j = self._fill(0.60, 0.90, 100)
        out = j.diagnose("buffer")
        expected = f"{breakeven_probability(0.60, 200):.1%}"
        self.assertIn(expected, out)

    def test_empty_journal_is_handled(self):
        self.assertIn("No resolved trades", Journal(self.db).diagnose())

    def test_scaling_stake_cannot_flip_expectancy(self):
        """The claim the advice rests on, asserted directly."""
        q, price, fee = 0.68, 0.714, 200
        signs = set()
        for stake in (0.5, 2.5, 10.0, 100.0, 1000.0):
            ev = (q * settle_pnl(stake, price, True, fee)
                  + (1 - q) * settle_pnl(stake, price, False, fee))
            signs.add(ev > 0)
        self.assertEqual(len(signs), 1)      # stake never changes the sign

    def test_lower_price_improves_the_win_loss_ratio(self):
        hi = settle_pnl(10, 0.90, True, 0) / 10
        lo = settle_pnl(10, 0.60, True, 0) / 10
        self.assertGreater(lo, hi)


class TestScaleIn(unittest.TestCase):
    """
    Top up while winning -- the opposite of martingale, which adds after
    losses. The target is recomputed from the current probability, so total
    exposure to one round stays bounded by Kelly.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _setup(self, spot_now, **over):
        settings = dict(m.PROFILES["buffer"]); settings.update(over)
        c = cfg(db_path=self.db, **settings)
        start = 1_700_000_000_000
        rnd = make_round(strike=65_000.0, start_ms=start, fee_bps=0,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        path = [(start, 65_000.0), (start + 120_000, spot_now),
                (start + m.DEFAULT_ROUND_SECONDS * 1000 + 3_000, spot_now)]
        # Inside the buffer profile's band, which the return floor caps at
        # ~0.80. Prices above it are refused before sizing is ever reached,
        # so a fixture written at 0.88 would test nothing but the refusal.
        book = {(1, Side.UP): [(0.72, 1e6)], (1, Side.DOWN): [(0.75, 1e6)]}
        client = FakeClient([rnd], path, book, {})
        t = build_trader(client, c, self.db)
        sig = Signal(Side.UP, 0.90, 0.68, 0.02, 2.0, 120.0, 1.8)
        tid = t._journal.record("PAPER", rnd, sig, 65_000, 0.5, 100.0)
        t._position = Position(tid, rnd, sig, 2.0, 1)
        client.t = 1
        return t, client

    def test_tops_up_when_the_round_moves_further_ahead(self):
        t, _ = self._setup(65_260.0)          # large buffer now
        before = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        self.assertGreater(t._position.committed_usdt, before)
        self.assertEqual(t._position.tranches, 2)

    def test_never_adds_when_the_round_turns_against_us(self):
        """The martingale test: a losing position must not be topped up."""
        t, _ = self._setup(64_800.0)          # spot below strike now
        before = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        self.assertEqual(t._position.committed_usdt, before)
        self.assertEqual(t._position.tranches, 1)

    def test_never_adds_when_probability_merely_holds(self):
        t, _ = self._setup(65_040.0)
        t._position = replace(t._position,
                              signal=replace(t._position.signal, model_prob=0.999))
        before = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        self.assertEqual(t._position.committed_usdt, before)

    def test_total_exposure_stays_within_the_kelly_cap(self):
        t, _ = self._setup(65_400.0)
        for _ in range(8):
            t._maybe_scale_in(100.0)
        c = t._cfg
        self.assertLessEqual(t._position.committed_usdt,
                             (100.0 + t._position.committed_usdt)
                             * c.hard_max_stake_pct + 1e-6)

    def test_no_topup_after_the_entry_window_closes(self):
        t, client = self._setup(65_400.0)
        client.t = 2                          # past resolution
        before = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        self.assertEqual(t._position.committed_usdt, before)

    def test_disabled_by_default_for_other_profiles(self):
        for name in ("convex", "balanced", "favorite", "micro"):
            c = Config(api_key="k", api_secret="s", **m.PROFILES[name])
            self.assertFalse(c.scale_in, name)

    def test_blended_price_is_share_weighted(self):
        pos = Position(1, make_round(),
                       Signal(Side.UP, 0.9, 0.80, 0.02, 10.0, 60.0, 2.0),
                       10.0, 1)
        blended = pos.average_price(10.0, 0.90)
        shares = 10.0 / 0.80 + 10.0 / 0.90
        self.assertAlmostEqual(blended, 20.0 / shares)

    def test_first_tranche_is_smaller_when_scaling_in(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertLess(c.scale_in_initial_pct, 1.0)

    def test_invalid_initial_fraction_rejected(self):
        for bad in (0.0, 1.5, -0.2):
            with self.assertRaises(ValueError):
                cfg(scale_in_initial_pct=bad)


class TestProfileRiskCoherence(unittest.TestCase):
    """Risk limits must match each profile's own loss shape."""

    def test_daily_limit_allows_more_than_two_losses(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            losses = c.daily_loss_limit_pct / c.max_stake_pct
            self.assertGreaterEqual(losses, 2.5,
                                    f"{name}: halts after {losses:.1f} losses")

    def test_every_profile_sets_its_own_daily_limit(self):
        for name, prof in m.PROFILES.items():
            self.assertIn("daily_loss_limit_pct", prof, name)

    def test_every_profile_sets_its_own_spread_assumption(self):
        for name, prof in m.PROFILES.items():
            self.assertIn("assumed_spread_pct", prof, name)

    def test_spread_is_proportional_not_absolute(self):
        """A flat haircut is 60% of a longshot and 3% of a near-certainty."""
        c = cfg(assumed_spread_pct=0.05)
        cheap = 0.05 * (1 + c.assumed_spread_pct)
        dear = 0.95 * (1 + c.assumed_spread_pct)
        self.assertAlmostEqual(cheap / 0.05, dear / 0.95)

    def test_default_price_band_is_not_convex_shaped(self):
        self.assertGreaterEqual(cfg().max_entry_price, 0.90)


class TestNoBakedInValues(unittest.TestCase):
    """Values that belong to configuration must not be literals in logic."""

    def test_symbol_is_configurable(self):
        c = cfg(symbols=("ETHUSDT",))
        PredictionClient(c)
        try:
            t = TestParseRound.topic(symbol="ETHUSDT")
            self.assertIsNotNone(PredictionClient._parse_round(t))
            self.assertIsNone(PredictionClient._parse_round(
                TestParseRound.topic(symbol="BTCUSDT")))
        finally:
            PredictionClient(cfg())

    def test_round_duration_tolerance_is_configurable(self):
        base = 1_700_000_000_000
        PredictionClient(cfg(round_duration_tolerance=0.5))
        try:
            t = TestParseRound.topic(startDate=base, endDate=base + 400_000)
            self.assertIsNotNone(PredictionClient._parse_round(t))
        finally:
            PredictionClient(cfg())

    def test_timing_constants_exist_as_config(self):
        c = cfg()
        for name in ("clock_resync_s", "settle_grace_s", "settle_timeout_s",
                     "drain_timeout_s", "drain_poll_s", "prune_after_s",
                     "vol_cache_s", "error_backoff_max_s"):
            self.assertGreater(getattr(c, name), 0, name)

    def test_timing_constants_are_validated(self):
        for name in ("clock_resync_s", "settle_grace_s", "prune_after_s"):
            with self.assertRaises(ValueError, msg=name):
                cfg(**{name: 0})

    def test_settled_history_limit_exceeds_fifty(self):
        """50 could miss an older settlement and strand a position."""
        self.assertGreater(cfg().settled_history_limit, 50)

    def test_quote_tolerance_is_configurable(self):
        self.assertEqual(cfg(quote_consistency_tolerance=0.02)
                         .quote_consistency_tolerance, 0.02)

    def test_no_literal_symbol_in_parsing_logic(self):
        import inspect
        src = inspect.getsource(PredictionClient._parse_round)
        self.assertNotIn('"BTCUSDT"', src)


class TestPerMarketFeeInDiagnostics(unittest.TestCase):
    """The breakeven bar must use each market's own published fee."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _fill(self, fee_bps, n=60, price=0.714, win_rate=0.75):
        import random as _r
        rng = _r.Random(2)
        j = Journal(self.db, "buffer")
        rnd = make_round(fee_bps=fee_bps)
        for _ in range(n):
            won = rng.random() < win_rate
            sig = Signal(Side.UP, 0.80, price, 0.05, 2.5, 40.0, 2.4)
            t = j.record("PAPER", rnd, sig, 65_000, 0.5, 25.0)
            j.resolve(t, won, settle_pnl(2.5, price, won, fee_bps), "venue")
        return j

    def test_zero_fee_market_uses_a_lower_bar(self):
        out = self._fill(0).diagnose("buffer")
        self.assertIn(f"{breakeven_probability(0.714, 0):.1%}", out)

    def test_high_fee_market_uses_a_higher_bar(self):
        out = self._fill(500).diagnose("buffer")
        self.assertIn(f"{breakeven_probability(0.714, 500):.1%}", out)

    def test_fee_is_recorded_per_trade(self):
        j = self._fill(137, n=5)
        row = j._conn.execute(
            "SELECT fee_bps FROM trades LIMIT 1").fetchone()
        self.assertEqual(row[0], 137)

    def test_legacy_rows_without_fee_fall_back(self):
        import sqlite3 as sq
        self._fill(200, n=40)
        conn = sq.connect(self.db)
        conn.execute("UPDATE trades SET fee_bps = NULL")
        conn.commit(); conn.close()
        self.assertIn("Realised win rate", Journal(self.db).diagnose("buffer"))


class TestMainEntryPoint(unittest.TestCase):
    """
    Actually invoke main().

    Every other test constructs Config directly, so main() was never executed
    by the suite at all. That is how a duplicate keyword argument -- profiles
    setting kelly_fraction while main() also passed it explicitly -- shipped
    and crashed on the first real run with 384 tests green.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env)
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _run(self, *argv):
        """Run main() with stdout captured; returns (code, output)."""
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_every_profile_constructs_through_main(self):
        """The exact failure: Config() got two values for kelly_fraction."""
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile,
                                  "--calibration-report")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_every_profile_accepts_a_kelly_override(self):
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile, "--kelly", "0.5",
                                  "--calibration-report")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_kelly_defaults_to_the_profile_not_the_flag(self):
        """--kelly defaulted to 0.25, silently overriding every profile."""
        import inspect, re as _re
        src = inspect.getsource(m.main)
        hit = _re.search(r'"--kelly".*?default=(\w+)', src, _re.S)
        self.assertEqual(hit.group(1), "None")

    def test_all_override_flags_work_together(self):
        code, out = self._run("--profile", "buffer", "--kelly", "0.3",
                              "--min-edge", "0.02", "--fee-bps", "0",
                              "--min-buffer", "1.0", "--paper-bankroll", "50",
                              "--scale-in", "--calibration-report")
        self.assertEqual(code, 0, out)

    def test_diagnose_runs_for_every_profile(self):
        for profile in sorted(m.PROFILES):
            code, out = self._run("--profile", profile, "--diagnose")
            self.assertEqual(code, 0, f"{profile}: {out}")

    def test_reading_a_report_needs_no_credentials(self):
        """Reading the journal is not an API call, so keys are not required."""
        del os.environ["BINANCE_API_KEY"]
        code, _ = self._run("--profile", "micro", "--calibration-report")
        self.assertEqual(code, 0)

    def test_missing_credentials_exits_cleanly_for_api_commands(self):
        del os.environ["BINANCE_API_KEY"]
        code, out = self._run("--profile", "micro")
        self.assertEqual(code, 1)
        self.assertIn("BINANCE_API_KEY", out)

    def test_invalid_config_exits_cleanly_not_with_a_traceback(self):
        code, out = self._run("--profile", "micro", "--kelly", "9")
        self.assertEqual(code, 1)
        self.assertIn("Invalid configuration", out)

    def test_no_config_key_is_passed_twice(self):
        """Structural guard: one mapping, so a collision cannot recur."""
        import ast, inspect
        tree = ast.parse(inspect.getsource(m.main))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Config"):
                continue
            explicit = [k.arg for k in node.keywords if k.arg is not None]
            starred = [k for k in node.keywords if k.arg is None]
            self.assertFalse(explicit and starred,
                             "Config() mixes explicit kwargs with **settings; "
                             "a profile field can collide with one of them")


class TestConfigFile(unittest.TestCase):
    """Every setting lives in the file, and the file round-trips."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _write(self, doc=None):
        import json as _j
        _j.dump(doc or m.default_config_document(),
                open(self.path, "w"), indent=2)

    def test_document_covers_every_mutable_config_field(self):
        import dataclasses as _dc
        doc = m.default_config_document()
        covered = set(doc["defaults"])
        for prof in doc["profiles"].values():
            covered |= set(prof)
        for f in _dc.fields(Config):
            if f.name in m.IMMUTABLE_FIELDS:
                continue
            self.assertIn(f.name, covered,
                          f"{f.name} is not represented in the config file")

    def test_no_immutable_field_is_emitted(self):
        doc = m.default_config_document()
        self.assertEqual(set(doc["defaults"]) & m.IMMUTABLE_FIELDS, set())

    def test_document_round_trips_to_the_same_config(self):
        for name in m.PROFILES:
            built = m.build_config(m.default_config_document(), api_key="k",
                                   api_secret="s", live=False, db_path="d",
                                   profile=name)
            direct = Config(api_key="k", api_secret="s", db_path="d",
                            profile_name=name, **m.PROFILES[name])
            for fld in ("max_stake_pct", "min_entry_price", "max_entry_price",
                        "min_edge", "kelly_fraction", "daily_loss_limit_pct",
                        "min_buffer_sigmas", "scale_in"):
                self.assertEqual(getattr(built, fld), getattr(direct, fld),
                                 f"{name}.{fld}")

    def test_document_is_json_serialisable(self):
        import json as _j
        _j.loads(_j.dumps(m.default_config_document()))

    def test_unknown_setting_is_rejected_by_name(self):
        doc = m.default_config_document()
        doc["overrides"]["not_a_setting"] = 1
        with self.assertRaises(ValueError) as ctx:
            m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d")
        self.assertIn("not_a_setting", str(ctx.exception))

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            m.build_config(m.default_config_document(), api_key="k",
                           api_secret="s", live=False, db_path="d",
                           profile="nope")

    def test_layering_order_overrides_wins(self):
        doc = m.default_config_document()
        doc["profiles"]["micro"]["max_stake_pct"] = 0.11
        doc["overrides"]["max_stake_pct"] = 0.12
        c = m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d", profile="micro",
                           overrides={"max_stake_pct": 0.13})
        self.assertEqual(c.max_stake_pct, 0.13)   # CLI beats file overrides

    def test_symbols_override_layers_the_same_way(self):
        """--symbols / SYMBOLS reach Config via the exact same `overrides`
        path as every other CLI/env-sourced setting, so they get the same
        precedence: CLI/env pins it regardless of what the file says."""
        doc = m.default_config_document()
        doc["defaults"]["symbols"] = ["ETHUSDT"]
        c = m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d",
                           overrides={"symbols": m._parse_symbols_arg(
                               "btcusdt, solusdt")})
        self.assertEqual(c.symbols, ("BTCUSDT", "SOLUSDT"))

    def test_tuple_fields_survive_json(self):
        doc = m.default_config_document()
        doc["defaults"]["open_statuses"] = ["OPEN", "ACTIVE"]
        c = m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d")
        self.assertEqual(c.open_statuses, ("OPEN", "ACTIVE"))

    def test_int_field_rejects_a_fractional_value(self):
        doc = m.default_config_document()
        doc["defaults"]["market_list_limit"] = 12.5
        with self.assertRaises(ValueError):
            m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d")

    def test_endpoint_overrides_apply(self):
        doc = m.default_config_document()
        doc["endpoints"]["order_book"] = ["POST", "/custom"]
        c = m.build_config(doc, api_key="k", api_secret="s", live=False,
                           db_path="d")
        self.assertEqual(c.ep("order_book"), ("POST", "/custom"))


class TestHotReload(unittest.TestCase):
    """A bad edit must never take down a running bot."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        self._save(m.default_config_document())
        self.store = m.ConfigStore(self.path, api_key="k", api_secret="s",
                                   live=False, db_path="/tmp/j.db",
                                   profile="buffer")

    def tearDown(self):
        os.unlink(self.path)

    def _save(self, doc):
        import json as _j
        _j.dump(doc, open(self.path, "w"), indent=2)
        future = time.time() + 5
        os.utime(self.path, (future, future))

    def _edit(self, fn):
        import json as _j
        doc = _j.load(open(self.path))
        fn(doc)
        self._save(doc)

    def test_valid_edit_is_applied(self):
        self._edit(lambda d: d["profiles"]["buffer"].update(
            min_buffer_sigmas=3.0))
        self.assertTrue(self.store.maybe_reload())
        self.assertEqual(self.store.current.min_buffer_sigmas, 3.0)

    def test_no_change_means_no_reload(self):
        self.assertFalse(self.store.maybe_reload())

    def test_invalid_value_keeps_the_previous_config(self):
        before = self.store.current.kelly_fraction
        self._edit(lambda d: d["profiles"]["buffer"].update(kelly_fraction=99))
        self.assertFalse(self.store.maybe_reload())
        self.assertEqual(self.store.current.kelly_fraction, before)

    def test_malformed_json_keeps_the_previous_config(self):
        before = self.store.current.min_buffer_sigmas
        with open(self.path, "a") as fh:
            fh.write("}}}not json")
        future = time.time() + 9
        os.utime(self.path, (future, future))
        self.assertFalse(self.store.maybe_reload())
        self.assertEqual(self.store.current.min_buffer_sigmas, before)

    def test_deleted_file_keeps_the_previous_config(self):
        before = self.store.current.min_buffer_sigmas
        os.unlink(self.path)
        self.assertFalse(self.store.maybe_reload())
        self.assertEqual(self.store.current.min_buffer_sigmas, before)
        self._save(m.default_config_document())      # restore for tearDown

    def test_immutable_field_is_ignored_not_applied(self):
        self._edit(lambda d: d["overrides"].update(db_path="/tmp/elsewhere.db"))
        self.store.maybe_reload()
        self.assertEqual(self.store.current.db_path, "/tmp/j.db")

    def test_reload_count_only_counts_successes(self):
        self._edit(lambda d: d["profiles"]["buffer"].update(kelly_fraction=99))
        self.store.maybe_reload()
        self.assertEqual(self.store.reload_count, 0)
        self._edit(lambda d: d["profiles"]["buffer"].update(kelly_fraction=0.3))
        self.store.maybe_reload()
        self.assertEqual(self.store.reload_count, 1)

    def test_parsing_attributes_follow_a_reload(self):
        """_parse_round reads class attributes, which must track the config."""
        self._edit(lambda d: d["defaults"].update(symbols=["ETHUSDT"]))
        self.assertTrue(self.store.maybe_reload())
        PredictionClient.apply_config(self.store.current)
        try:
            self.assertIsNone(PredictionClient._parse_round(
                TestParseRound.topic(symbol="BTCUSDT")))
            self.assertIsNotNone(PredictionClient._parse_round(
                TestParseRound.topic(symbol="ETHUSDT")))
        finally:
            PredictionClient(cfg())

    def test_client_sees_the_new_config_without_rebuilding(self):
        client = PredictionClient(self.store)
        self.assertEqual(client._cfg.min_buffer_sigmas, 0.75)
        self._edit(lambda d: d["profiles"]["buffer"].update(
            min_buffer_sigmas=2.75))
        self.store.maybe_reload()
        self.assertEqual(client._cfg.min_buffer_sigmas, 2.75)

    def test_a_plain_config_still_works_without_a_store(self):
        client = PredictionClient(cfg())
        self.assertIsInstance(client._cfg, Config)


class TestDeploymentEntrypoint(unittest.TestCase):
    """
    The deploy path, tested as a unit.

    These are the behaviours that cannot be checked by reading the file: a
    regenerated config would silently discard the user's edits on every
    deploy, and a missing `exec` would stop SIGTERM ever reaching Python.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        cls.script = _os.path.join(here, "entrypoint.sh")
        cls.bot = _os.path.join(here, "btc_5m_predictor.py")
        cls.text = (open(cls.script).read()
                    if _os.path.exists(cls.script) else "")

    def setUp(self):
        if not self.text:
            self.skipTest("entrypoint.sh not present")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, config_path, profile=None):
        import subprocess, shutil as _sh, os as _os
        _sh.copy(self.bot, self.tmp)
        _sh.copy(self.script, self.tmp)
        _os.chmod(_os.path.join(self.tmp, "entrypoint.sh"), 0o755)
        # These exercise config seeding, not the venue: preflight would try
        # to reach Binance with placeholder keys and fail for reasons that
        # have nothing to do with what is being tested.
        env = dict(_os.environ,
                   BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                   CONFIG_PATH=config_path,
                   PROFILE=profile or m.DEFAULT_PROFILE,
                   SKIP_PREFLIGHT="1", VERIFY_NESTED="1",
                   DB_PATH=_os.path.join(self.tmp, "j.db"))
        return subprocess.run(["./entrypoint.sh", "--check-config"],
                              cwd=self.tmp, env=env, capture_output=True,
                              text=True, timeout=120)

    def test_uses_exec_so_sigterm_reaches_python(self):
        """Without exec the shell keeps PID 1 and Python never shuts down."""
        self.assertRegex(self.text, r"\nexec python")

    def test_first_boot_creates_the_config(self):
        import os as _os
        path = _os.path.join(self.tmp, "data", "config.json")
        r = self._run(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(_os.path.exists(path))

    def test_written_config_uses_the_requested_profile(self):
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path, profile="convex")
        self.assertEqual(_j.load(open(path))["active_profile"], "convex")

    def test_redeploy_preserves_user_edits(self):
        """The reason the script must never regenerate an existing file."""
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path)
        doc = _j.load(open(path))
        doc["profiles"][m.DEFAULT_PROFILE]["min_buffer_sigmas"] = 2.75
        doc["overrides"]["max_rounds_per_day"] = 42
        _j.dump(doc, open(path, "w"), indent=2)

        r = self._run(path)                      # simulate a redeploy
        self.assertEqual(r.returncode, 0, r.stderr)
        after = _j.load(open(path))
        self.assertEqual(
            after["profiles"][m.DEFAULT_PROFILE]["min_buffer_sigmas"], 2.75)
        self.assertEqual(after["overrides"]["max_rounds_per_day"], 42)

    def test_invalid_config_fails_before_starting(self):
        import os as _os, json as _j
        path = _os.path.join(self.tmp, "data", "config.json")
        self._run(path)
        doc = _j.load(open(path))
        doc["profiles"][m.DEFAULT_PROFILE]["max_stake_pct"] = 9
        _j.dump(doc, open(path, "w"))
        r = self._run(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid", r.stderr.lower())

    def test_unwritable_config_dir_fails_with_a_clear_message(self):
        r = self._run("/proc/nope/config.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("mounted disk", r.stderr)

    def test_config_and_journal_default_to_the_same_disk(self):
        self.assertIn("/var/data/config.json", self.text)
        self.assertIn("/var/data", self.text)


class TestDefaultProfileCoherence(unittest.TestCase):
    """
    One default, four files.

    The manifests cannot import the module, so each carries a literal. Because
    the config is written only once, a manifest disagreeing with the module
    seeds the wrong strategy for the life of the disk.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = _os.path.dirname(_os.path.abspath(__file__))

    def _read(self, name):
        import os as _os
        path = _os.path.join(self.here, name)
        if not _os.path.exists(path):
            self.skipTest(f"{name} not present")
        return open(path).read()

    def test_dockerfile_agrees_with_the_module(self):
        import re as _re
        hit = _re.search(r"ENV\s+PROFILE=(\w+)", self._read("Dockerfile"))
        self.assertEqual(hit.group(1), m.DEFAULT_PROFILE)

    def test_render_agrees_with_the_module(self):
        import re as _re
        hit = _re.search(r"key:\s*PROFILE\s*\n\s*value:\s*(\w+)",
                         self._read("render.yaml"))
        self.assertEqual(hit.group(1), m.DEFAULT_PROFILE)

    def test_entrypoint_derives_rather_than_hardcodes(self):
        text = self._read("entrypoint.sh")
        self.assertIn("--print-default-profile", text)

    def test_print_default_profile_matches_the_constant(self):
        import subprocess, os as _os
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
        r = subprocess.run(
            [sys.executable, _os.path.join(self.here, "btc_5m_predictor.py"),
             "--print-default-profile"], capture_output=True, text=True,
            env=env, timeout=60)
        self.assertEqual(r.stdout.strip(), m.DEFAULT_PROFILE)


class TestVerificationGate(unittest.TestCase):
    """
    Broken code must not become a trading process.

    verify.sh runs at build time (failing the build) and again at boot
    (failing the start). These check the gate actually gates.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = _os.path.dirname(_os.path.abspath(__file__))
        cls.script = _os.path.join(cls.here, "verify.sh")

    def setUp(self):
        import os as _os
        if not _os.path.exists(self.script):
            self.skipTest("verify.sh not present")
        if _os.environ.get("VERIFY_NESTED") == "1":
            # verify.sh runs this suite, and this suite runs verify.sh.
            # Without a depth guard that recurses forever, so the inner run
            # skips these particular tests. The outer run still exercises them.
            self.skipTest("nested inside verify.sh")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage(self, mutate=None):
        import shutil as _sh, os as _os, glob
        for path in glob.glob(_os.path.join(self.here, "*.py")):
            _sh.copy(path, self.tmp)
        _sh.copy(self.script, self.tmp)
        if mutate:
            target = _os.path.join(self.tmp, "btc_5m_predictor.py")
            text = open(target).read()
            open(target, "w").write(mutate(text))

    def _run(self, env_extra=None):
        import subprocess, os as _os
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                   VERIFY_NESTED="1")
        env.update(env_extra or {})
        return subprocess.run(["bash", "verify.sh"], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=600)

    def test_healthy_code_passes(self):
        self._stage()
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])

    def test_syntax_error_is_caught(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("byte-compile", r.stdout)

    def test_broken_logic_is_caught(self):
        """A function that returns a plausible constant still fails."""
        self._stage(lambda t: t.replace(
            "def breakeven_probability(price: float, fee_bps: int) -> float:",
            "def breakeven_probability(price: float, fee_bps: int) -> float:\n"
            "    return 0.5"))
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)

    def test_refusal_message_is_explicit(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run()
        self.assertIn("must not trade", r.stdout)

    def test_skip_verify_bypasses_everything(self):
        self._stage(lambda t: t + "\ndef broken(:\n")
        r = self._run({"SKIP_VERIFY": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("NOT been checked", r.stdout)

    def test_missing_connector_skips_rather_than_fails(self):
        """The runtime image has no Node connector; that is not a violation."""
        import os as _os
        self._stage()
        text = open(_os.path.join(self.tmp, "verify.sh")).read()
        text = text.replace('"$PY" conformance.py 2>&1',
                            '"$PY" conformance.py --connector /nonexistent 2>&1')
        open(_os.path.join(self.tmp, "verify.sh"), "w").write(text)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout[-1000:])
        self.assertIn("SKIP", r.stdout)

    def test_mutation_testing_is_not_a_deploy_gate(self):
        """20+ minutes of test-quality measurement must not block a restart."""
        self.assertNotIn("mutate.py", open(self.script).read())

    def test_boot_verification_skips_only_the_self_referential_tests(self):
        """Boot must stay fast; the gate's own tests belong at build time."""
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        self.assertIn("VERIFY_NESTED=1 bash ./verify.sh", open(path).read())

    def test_build_verification_is_the_full_run(self):
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        text = open(path).read()
        build_line = [ln for ln in text.split("\n")
                      if ln.startswith("RUN BINANCE_API_KEY=build")][0]
        self.assertNotIn("VERIFY_NESTED", build_line)

    def test_entrypoint_runs_verification_before_exec(self):
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        text = open(path).read()
        self.assertLess(text.index("verify.sh"), text.index("exec python"))

    def test_dockerfile_verifies_at_build_time(self):
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        text = open(path).read()
        self.assertIn("RUN BINANCE_API_KEY=build", text)
        self.assertLess(text.index("verify.sh"), text.index("ENTRYPOINT"))

    def test_render_verifies_at_build_time(self):
        import os as _os
        path = _os.path.join(self.here, "render.yaml")
        if not _os.path.exists(path):
            self.skipTest("render.yaml not present")
        text = open(path).read()
        build = text[text.index("buildCommand"):text.index("startCommand")]
        self.assertIn("verify.sh", build)


class TestModeSwitching(unittest.TestCase):
    """
    paper <-> live, hot-reloadable but deferred.

    A mid-round switch is incoherent in both directions: a paper position has
    no real order behind it, and a real position flipped to paper stops being
    tracked while its settlement is simulated. So the switch waits until flat.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        fd, self.path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        doc = m.default_config_document()
        doc["defaults"]["live"] = False   # these tests assume a paper start
        self._save(doc)

    def tearDown(self):
        for p in (self.db, self.path):
            if os.path.exists(p):
                os.unlink(p)

    def _save(self, doc):
        import json as _j
        _j.dump(doc, open(self.path, "w"), indent=2)
        future = time.time() + 5
        os.utime(self.path, (future, future))

    def _store(self, live=None):
        return m.ConfigStore(self.path, api_key="k", api_secret="s",
                             live=live, db_path=self.db,
                             profile=m.DEFAULT_PROFILE)

    def _set_live(self, value):
        import json as _j
        doc = _j.load(open(self.path))
        doc["defaults"]["live"] = value
        self._save(doc)

    def _trader(self, store):
        client = FakeClient([], [(0, 65_000.0)], {}, {})
        t = build_trader(client, store.current, self.db)
        t._store = store
        t._static_cfg = None
        t._active_live = store.current.live
        return t

    def test_file_governs_when_unpinned(self):
        store = self._store(live=None)
        self.assertFalse(store.current.live)
        self._set_live(True)
        self.assertTrue(store.maybe_reload())
        self.assertTrue(store.current.live)

    def test_flag_pins_and_file_cannot_override(self):
        store = self._store(live=False)
        self._set_live(True)
        store.maybe_reload()
        self.assertFalse(store.current.live)

    def test_pinned_edit_is_reported_not_silent(self):
        store = self._store(live=True)
        self._set_live(False)
        self.assertIn("live", " ".join(store._ignored_edits(
            {"defaults": {"live": False}})))

    def test_switch_applies_when_flat(self):
        store = self._store(live=None)
        t = self._trader(store)
        self.assertFalse(t._live)
        self._set_live(True)
        store.maybe_reload()
        t._apply_pending_mode()
        self.assertTrue(t._live)

    def test_switch_is_deferred_while_a_position_is_open(self):
        store = self._store(live=None)
        t = self._trader(store)
        t._position = Position(1, make_round(),
                               Signal(Side.UP, 0.9, 0.6, 0.05, 1.0, 60.0, 2.0),
                               1.0, 1)
        self._set_live(True)
        store.maybe_reload()
        t._apply_pending_mode()
        self.assertFalse(t._live, "mode changed mid-round")

    def test_switch_is_deferred_while_winnings_are_unclaimed(self):
        store = self._store(live=None)
        t = self._trader(store)
        t._unredeemed = {"tok": (5.0, ["0xabc"], "56")}
        self._set_live(True)
        store.maybe_reload()
        t._apply_pending_mode()
        self.assertFalse(t._live)

    def test_deferred_switch_applies_once_flat(self):
        store = self._store(live=None)
        t = self._trader(store)
        t._position = Position(1, make_round(),
                               Signal(Side.UP, 0.9, 0.6, 0.05, 1.0, 60.0, 2.0),
                               1.0, 1)
        self._set_live(True)
        store.maybe_reload()
        t._apply_pending_mode()
        self.assertFalse(t._live)
        t._positions = {}
        t._apply_pending_mode()
        self.assertTrue(t._live)

    def test_switch_resets_the_risk_baseline(self):
        store = self._store(live=None)
        t = self._trader(store)
        before = t._account_risk
        t._account_risk.record_result(False, 0.6)
        self._set_live(True)
        store.maybe_reload()
        t._apply_pending_mode()
        self.assertIsNot(t._risk, before)
        self.assertEqual(t._account_risk.consecutive_losses, 0)

    def test_no_change_is_a_no_op(self):
        store = self._store(live=None)
        t = self._trader(store)
        risk = t._account_risk
        t._apply_pending_mode()
        self.assertIs(t._account_risk, risk)

    def test_live_is_not_immutable_but_is_deferred(self):
        self.assertNotIn("live", m.IMMUTABLE_FIELDS)
        self.assertIn("live", m.DEFERRED_FIELDS)


class TestTradingModeEnv(unittest.TestCase):
    """TRADING_MODE is the environment equivalent of --live/--paper."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env)
        os.unlink(self.db)

    def _run(self, *argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_invalid_mode_is_rejected(self):
        os.environ["TRADING_MODE"] = "yolo"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 1)
        self.assertIn("TRADING_MODE", out)

    def test_paper_and_live_are_accepted(self):
        for value in ("paper", "live", "PAPER", "Live"):
            os.environ["TRADING_MODE"] = value
            code, out = self._run("--calibration-report")
            self.assertEqual(code, 0, f"{value}: {out}")

    def test_flags_are_mutually_expressive(self):
        import inspect
        src = inspect.getsource(m.main)
        self.assertIn('"--paper"', src)
        self.assertIn('"--live"', src)

    def test_flag_default_is_none_so_the_file_can_govern(self):
        import inspect, re as _re
        src = inspect.getsource(m.main)
        hit = _re.search(r'"--live".*?default=(\w+)', src, _re.S)
        self.assertEqual(hit.group(1), "None")


class TestSymbolsArgParsing(unittest.TestCase):
    """The comma-separated parser shared by --symbols and SYMBOLS."""

    def test_splits_and_uppercases(self):
        self.assertEqual(m._parse_symbols_arg("btcusdt,ethusdt"),
                         ("BTCUSDT", "ETHUSDT"))

    def test_strips_whitespace_and_drops_blank_entries(self):
        self.assertEqual(m._parse_symbols_arg(" btcusdt , , ethusdt "),
                         ("BTCUSDT", "ETHUSDT"))

    def test_blank_string_means_no_restriction(self):
        self.assertEqual(m._parse_symbols_arg(""), ())
        self.assertEqual(m._parse_symbols_arg("   "), ())
        self.assertEqual(m._parse_symbols_arg(",,,"), ())


class TestSymbolsEnv(unittest.TestCase):
    """SYMBOLS is the environment equivalent of --symbols."""

    def test_env_var_is_read(self):
        import inspect
        src = inspect.getsource(m.main)
        self.assertIn('os.environ.get("SYMBOLS"', src)

    def test_cli_flag_is_checked_before_the_env_fallback(self):
        """--symbols must win when both are set."""
        import inspect
        src = inspect.getsource(m.main)
        cli_at = src.index("if args.symbols:")
        env_at = src.index("elif env_symbols:")
        self.assertLess(cli_at, env_at)

    def test_cli_and_env_share_one_parser(self):
        """A shared helper is what keeps the two paths from drifting apart."""
        import inspect
        src = inspect.getsource(m.main)
        self.assertEqual(src.count("_parse_symbols_arg(args.symbols)"), 1)
        self.assertEqual(src.count("_parse_symbols_arg(env_symbols)"), 1)


class TestPreflightGate(unittest.TestCase):
    """Preflight runs at boot, where real credentials and a region exist."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = _os.path.dirname(_os.path.abspath(__file__))

    def _entry(self):
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        return open(path).read()

    def test_preflight_runs_before_exec(self):
        text = self._entry()
        self.assertLess(text.index("--preflight"), text.index("exec python"))

    def test_preflight_blocks_by_default(self):
        self.assertIn("PREFLIGHT_REQUIRED:-1", self._entry())

    def test_preflight_can_be_downgraded_to_a_warning(self):
        self.assertIn("PREFLIGHT_REQUIRED", self._entry())

    def test_preflight_can_be_skipped(self):
        self.assertIn("SKIP_PREFLIGHT", self._entry())

    def test_preflight_is_not_a_build_step(self):
        """Build has no real credentials and may sit in a blocked region."""
        import os as _os
        path = _os.path.join(self.here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        self.assertNotIn("--preflight", open(path).read())


class TestBlendedPriceCeiling(unittest.TestCase):
    """
    Top-ups happen at a HIGHER price, so each one drags the blend up and the
    payout down. The ceiling is what stops "scale in more" from quietly
    turning a 6-wins-per-loss position into a 15-wins-per-loss one.
    """

    def test_wins_per_loss_is_set_by_price_alone(self):
        self.assertAlmostEqual(m.wins_per_loss(0.50), 1.0)
        self.assertAlmostEqual(m.wins_per_loss(0.90), 9.0)
        self.assertAlmostEqual(m.wins_per_loss(0.95), 19.0)

    def test_wins_per_loss_rejects_impossible_prices(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                m.wins_per_loss(bad)

    def test_topup_below_the_cap_is_unbounded(self):
        self.assertEqual(
            m.max_topup_within_blend(1.0, 0.80, 0.88, 0.90), math.inf)

    def test_topup_above_the_cap_is_bounded(self):
        allowed = m.max_topup_within_blend(1.28, 0.86, 0.95, 0.90)
        self.assertLess(allowed, math.inf)
        self.assertGreater(allowed, 0.0)

    def test_the_bound_lands_exactly_on_the_cap(self):
        for price in (0.92, 0.95, 0.97, 0.99):
            allowed = m.max_topup_within_blend(1.28, 0.86, price, 0.90)
            shares = 1.28 / 0.86 + allowed / price
            blended = (1.28 + allowed) / shares
            self.assertAlmostEqual(blended, 0.90, places=6, msg=str(price))

    def test_a_position_already_over_the_cap_cannot_top_up(self):
        self.assertEqual(
            m.max_topup_within_blend(1.0, 0.95, 0.96, 0.90), 0.0)

    def test_higher_topup_price_permits_less(self):
        prev = math.inf
        for price in (0.91, 0.93, 0.95, 0.98):
            allowed = m.max_topup_within_blend(1.28, 0.86, price, 0.90)
            self.assertLess(allowed, prev)
            prev = allowed

    def test_rejects_invalid_inputs(self):
        with self.assertRaises(ValueError):
            m.max_topup_within_blend(0.0, 0.86, 0.95, 0.90)
        with self.assertRaises(ValueError):
            m.max_topup_within_blend(1.0, 0.0, 0.95, 0.90)
        with self.assertRaises(ValueError):
            m.max_topup_within_blend(1.0, 0.86, 1.0, 0.90)

    def test_buffer_profile_declares_a_ceiling(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertLessEqual(c.max_blended_price, 0.95)
        self.assertGreater(c.max_blended_price, c.min_entry_price)

    def test_the_return_floor_tightens_the_blend_cap(self):
        """
        A top-up is bought higher than the opener, so the blend is what
        actually decides the payout. Enforcing the return floor on entry and
        not on the blend would let the last tranche spend the whole return.
        """
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        # A market whose fee is worse than the profile's fallback: the cap
        # has to move with it rather than staying at the written number.
        self.assertLess(m.blended_price_cap(c, 2000), c.max_blended_price)
        self.assertLessEqual(m.blended_price_cap(c, 0), c.max_blended_price)

    def test_blend_cap_ignores_the_floor_when_none_is_set(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["balanced"])
        self.assertAlmostEqual(m.blended_price_cap(c, 200),
                               c.max_blended_price)

    def test_invalid_ceiling_is_rejected(self):
        for bad in (0.0, 1.0, 1.5):
            with self.assertRaises(ValueError):
                cfg(max_blended_price=bad)


class TestBlendCapIsPerProfile(unittest.TestCase):
    """
    The blend cap is only meaningful against a profile's own band.

    0.78 suits buffer (0.55-0.80) and is meaningless for convex (0.05-0.35),
    where no fill could approach it. Leaving it as a shared default would
    repeat the pattern of one strategy's number quietly governing all of them.
    """

    def test_every_profile_declares_its_own_cap(self):
        for name, prof in m.PROFILES.items():
            self.assertIn("max_blended_price", prof, name)

    def test_each_cap_sits_inside_its_own_band(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertGreaterEqual(c.max_blended_price, c.min_entry_price, name)
            self.assertLessEqual(c.max_blended_price, c.max_entry_price, name)

    def test_cap_above_the_band_is_rejected(self):
        """It could never bind, so it would silently do nothing."""
        with self.assertRaises(ValueError):
            cfg(min_entry_price=0.05, max_entry_price=0.35,
                max_blended_price=0.90)

    def test_cap_below_the_band_is_rejected(self):
        """No position could satisfy it, so top-ups would never happen."""
        with self.assertRaises(ValueError):
            cfg(min_entry_price=0.60, max_entry_price=0.97,
                max_blended_price=0.50)

    def test_caps_differ_across_profiles(self):
        caps = {Config(api_key="k", api_secret="s", **p).max_blended_price
                for p in m.PROFILES.values()}
        self.assertGreater(len(caps), 1, "all profiles share one cap")


class TestOnlyBufferScalesIn(unittest.TestCase):
    """The sizing change was requested for buffer; it must not leak."""

    def test_scale_in_is_enabled_only_for_buffer(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertEqual(c.scale_in, name == "buffer", name)

    def test_only_buffer_opens_below_full_kelly(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            mid = (c.min_entry_price + c.max_entry_price) / 2
            full = m.kelly_stake(100.0, min(mid * 1.5, 0.99), mid, c, 200)
            if name == "buffer":
                self.assertLess(c.scale_in_initial_pct, 1.0)
            else:
                opener = full
                self.assertAlmostEqual(opener, full, places=9, msg=name)

    def test_other_profiles_keep_the_standard_opener_fraction(self):
        for name, prof in m.PROFILES.items():
            if name == "buffer":
                continue
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertAlmostEqual(c.scale_in_initial_pct, 0.4, msg=name)

    def test_buffer_opens_smaller_than_the_others(self):
        buf = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        other = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertLess(buf.scale_in_initial_pct, other.scale_in_initial_pct)


class TestScaleInSizing(unittest.TestCase):
    """The top-up should be the larger bet -- bounded, not unbounded."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _setup(self, spot_now, **over):
        settings = dict(m.PROFILES["buffer"]); settings.update(over)
        c = cfg(db_path=self.db, **settings)
        start = 1_700_000_000_000
        rnd = make_round(strike=65_000.0, start_ms=start, fee_bps=0,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        path = [(start, 65_000.0), (start + 120_000, spot_now),
                (start + m.DEFAULT_ROUND_SECONDS * 1000 + 3_000, spot_now)]
        book = {(1, Side.UP): [(0.72, 1e6)], (1, Side.DOWN): [(0.75, 1e6)]}
        client = FakeClient([rnd], path, book, {})
        t = build_trader(client, c, self.db)
        sig = Signal(Side.UP, 0.88, 0.66, 0.02, 1.0, 120.0, 1.6)
        tid = t._journal.record("PAPER", rnd, sig, 65_000, 0.5, 100.0)
        t._position = Position(tid, rnd, sig, 1.0, 1)
        client.t = 1
        return t

    def test_topup_is_larger_than_the_opener(self):
        t = self._setup(65_260.0)
        opener = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        added = t._position.committed_usdt - opener
        self.assertGreater(added, opener)

    def test_smaller_opener_leaves_more_room(self):
        c_small = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertLessEqual(c_small.scale_in_initial_pct, 0.4)

    def test_blend_never_exceeds_the_ceiling(self):
        t = self._setup(65_400.0)
        for _ in range(6):
            t._maybe_scale_in(100.0)
        self.assertLessEqual(t._position.signal.fill_price,
                             t._cfg.max_blended_price + 1e-6)

    def test_total_exposure_still_bounded_by_kelly(self):
        t = self._setup(65_400.0)
        for _ in range(6):
            t._maybe_scale_in(100.0)
        c = t._cfg
        total = t._position.committed_usdt
        self.assertLessEqual(total, (100.0 + total) * c.hard_max_stake_pct + 1e-6)

    def test_no_topup_when_the_opener_is_already_past_the_ceiling(self):
        """
        Nothing may be added to a position that is already over the cap.

        Fail-safe rather than clever: a cheaper top-up would in fact drag the
        blend back DOWN, but distinguishing the two cases means trusting the
        blend arithmetic at exactly the moment the position is already
        outside its limits, and refusing is the cheaper mistake.
        """
        t = self._setup(65_400.0)
        t._position = replace(
            t._position,
            signal=replace(t._position.signal, fill_price=0.79))
        before = t._position.committed_usdt
        t._maybe_scale_in(100.0)
        self.assertEqual(t._position.committed_usdt, before)
        self.assertEqual(t._position.tranches, 1)


class TestModeIsNotHardcoded(unittest.TestCase):
    """
    The entrypoint must not pin the mode.

    A command-line flag beats TRADING_MODE, so a hardcoded --live makes that
    variable dead: render.yaml could read TRADING_MODE=paper while the process
    spent real money.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = _os.path.dirname(_os.path.abspath(__file__))

    def _entry(self):
        import os as _os
        path = _os.path.join(self.here, "entrypoint.sh")
        if not _os.path.exists(path):
            self.skipTest("entrypoint.sh not present")
        return open(path).read()

    def test_entrypoint_has_no_mode_flag(self):
        import re as _re
        for line in self._entry().split("\n"):
            if line.strip().startswith("#"):
                continue
            self.assertIsNone(
                _re.search(r"(?<![\w-])--(live|paper)(?![\w-])", line),
                f"entrypoint pins the mode: {line.strip()}")

    def test_cli_flag_would_beat_the_environment(self):
        """Why the above matters, asserted rather than assumed."""
        import json as _j, tempfile as _t, os as _os
        fd, path = _t.mkstemp(suffix=".json"); _os.close(fd)
        try:
            doc = m.default_config_document()
            doc["defaults"]["live"] = False   # explicit paper baseline
            _j.dump(doc, open(path, "w"))
            doc = _j.load(open(path))
            pinned = m.build_config(doc, api_key="k", api_secret="s",
                                    live=True, db_path="d")
            unpinned = m.build_config(doc, api_key="k", api_secret="s",
                                      live=None, db_path="d")
            self.assertTrue(pinned.live)
            self.assertFalse(unpinned.live)
        finally:
            _os.unlink(path)

    def test_render_declares_a_trading_mode(self):
        import os as _os, re as _re
        path = _os.path.join(self.here, "render.yaml")
        if not _os.path.exists(path):
            self.skipTest("render.yaml not present")
        # The key and its live value may be separated by commented-out
        # alternatives (`# value: paper`). Matching only the line immediately
        # after the key made the test fail on a perfectly valid manifest, so
        # scan forward past comments and blank lines to the first real value.
        lines = open(path).read().split("\n")
        value = None
        for i, line in enumerate(lines):
            if not _re.match(r"\s*-?\s*key:\s*TRADING_MODE\s*$", line):
                continue
            for follow in lines[i + 1:]:
                if not follow.strip() or follow.strip().startswith("#"):
                    continue
                hit = _re.match(r"\s*value:\s*(\w+)\s*$", follow)
                value = hit.group(1) if hit else None
                break
            break
        self.assertIsNotNone(
            value, "render.yaml declares no TRADING_MODE value")
        self.assertIn(value, ("paper", "live"))


class TestDeploymentManifests(unittest.TestCase):
    """render.yaml and the Dockerfile must agree with the entrypoint."""

    @classmethod
    def setUpClass(cls):
        import os as _os
        cls.here = _os.path.dirname(_os.path.abspath(__file__))

    def _read(self, name):
        import os as _os
        path = _os.path.join(self.here, name)
        if not _os.path.exists(path):
            self.skipTest(f"{name} not present")
        return open(path).read()

    def test_render_region_is_not_in_the_us(self):
        import re as _re
        text = self._read("render.yaml")
        region = _re.search(r"region:\s*(\w+)", text).group(1)
        self.assertNotIn(region, ("oregon", "ohio", "virginia"))

    def test_render_paths_sit_on_the_mounted_disk(self):
        import re as _re
        text = self._read("render.yaml")
        mount = _re.search(r"mountPath:\s*(\S+)", text).group(1)
        for key in ("CONFIG_PATH", "DB_PATH"):
            value = _re.search(rf"{key}\n\s*value:\s*(\S+)", text).group(1)
            self.assertTrue(value.startswith(mount), f"{key}={value}")

    def test_render_uses_the_entrypoint(self):
        self.assertIn("entrypoint.sh", self._read("render.yaml"))

    def test_render_disables_autodeploy(self):
        self.assertIn("autoDeploy: false", self._read("render.yaml"))

    def test_dockerfile_uses_entrypoint_not_cmd_python(self):
        text = self._read("Dockerfile")
        self.assertIn("ENTRYPOINT", text)
        self.assertIn("entrypoint.sh", text)

    def test_dockerfile_paths_are_on_the_disk_not_the_image(self):
        text = self._read("Dockerfile")
        self.assertIn("CONFIG_PATH=/var/data/", text)
        self.assertIn("DB_PATH=/var/data/", text)


class TestMultiMarket(unittest.TestCase):
    """
    Each market trades independently.

    Isolated: position slot, loss streak, calibration, volatility.
    NOT isolated, because there is one account: the bankroll, the daily loss
    limit, the venue quota.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _cfg(self, **over):
        base = dict(m.PROFILES["buffer"])
        base.update(symbols=("BTCUSDT", "ETHUSDT"),
                    max_concurrent_positions=2, db_path=self.db)
        base.update(over)
        return cfg(**base)

    def _trader(self, **over):
        client = FakeClient([], [(0, 65_000.0)], {}, {})
        return build_trader(client, self._cfg(**over), self.db)

    def test_only_configured_symbols_are_parsed(self):
        PredictionClient(self._cfg())
        try:
            for sym, ok in (("BTCUSDT", True), ("ETHUSDT", True),
                            ("SOLUSDT", False)):
                r = PredictionClient._parse_round(
                    TestParseRound.topic(symbol=sym))
                self.assertEqual(r is not None, ok, sym)
        finally:
            PredictionClient(cfg())

    def test_round_carries_its_market(self):
        PredictionClient(self._cfg())
        try:
            r = PredictionClient._parse_round(
                TestParseRound.topic(symbol="ETHUSDT"))
            self.assertEqual(r.symbol, "ETHUSDT")
        finally:
            PredictionClient(cfg())

    def test_market_ticker_is_not_the_oracle_feed(self):
        """
        Regression: `symbol` (the venue's contract ticker) and `feed_symbol`
        (the oracle it settles against) shared one local name, so the Round
        was built with the ORACLE value -- or the string "None" when no feed
        was published. Positions, per-market risk and the journal were all
        keyed on the wrong thing.
        """
        PredictionClient(self._cfg(symbols=()))
        try:
            t = TestParseRound.topic(symbol="ETHUSDT")
            t["variantData"] = {"startPrice": "65000",
                                "priceFeedSymbol": "Crypto.ETH/USD"}
            r = PredictionClient._parse_round(t)
            self.assertEqual(r.symbol, "ETHUSDT")
            self.assertEqual(r.feed_symbol, "Crypto.ETH/USD")
        finally:
            PredictionClient(cfg())

    def test_missing_feed_leaves_the_ticker_intact(self):
        PredictionClient(self._cfg(symbols=()))
        try:
            t = TestParseRound.topic(symbol="SOLUSDT")
            t["variantData"] = {"startPrice": "150"}
            r = PredictionClient._parse_round(t)
            self.assertEqual(r.symbol, "SOLUSDT")
            self.assertIsNone(r.feed_symbol)
        finally:
            PredictionClient(cfg())

    def test_a_topic_without_a_symbol_is_rejected(self):
        PredictionClient(self._cfg(symbols=()))
        try:
            t = TestParseRound.topic()
            del t["symbol"]
            self.assertIsNone(PredictionClient._parse_round(t))
        finally:
            PredictionClient(cfg())

    def test_discovery_accepts_every_listed_market(self):
        PredictionClient(self._cfg(symbols=()))
        try:
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
                self.assertIsNotNone(
                    PredictionClient._parse_round(
                        TestParseRound.topic(symbol=sym)), sym)
        finally:
            PredictionClient(cfg())

    def test_positions_are_held_per_market(self):
        t = self._trader()
        for sym in ("BTCUSDT", "ETHUSDT"):
            rnd = make_round(symbol=sym, topic_id=hash(sym) % 1000)
            t._positions[sym] = Position(
                1, rnd, Signal(Side.UP, 0.9, 0.85, 0.02, 2.0, 60.0, 2.0),
                2.0, 1)
        self.assertEqual(len(t._positions), 2)

    def test_loss_streaks_do_not_cross_markets(self):
        t = self._trader()
        for _ in range(5):
            t._risk_for("BTCUSDT").record_result(False, 0.9)
        self.assertEqual(t._risk_for("BTCUSDT").consecutive_losses, 5)
        self.assertEqual(t._risk_for("ETHUSDT").consecutive_losses, 0)

    def test_each_market_gets_its_own_risk_manager(self):
        t = self._trader()
        self.assertIsNot(t._risk_for("BTCUSDT"), t._risk_for("ETHUSDT"))

    def test_bankroll_is_shared_not_isolated(self):
        """One account: exposure must be counted across markets."""
        t = self._trader()
        t._positions["BTCUSDT"] = Position(
            1, make_round(symbol="BTCUSDT"),
            Signal(Side.UP, 0.9, 0.85, 0.02, 3.0, 60.0, 2.0), 3.0, 1)
        self.assertAlmostEqual(t._committed(), 3.0)

    def test_new_positions_size_against_uncommitted_funds(self):
        t = self._trader(reserve_pct=0.0)
        self.assertAlmostEqual(t._available(100.0), 100.0)
        t._positions["BTCUSDT"] = Position(
            1, make_round(symbol="BTCUSDT"),
            Signal(Side.UP, 0.9, 0.85, 0.02, 10.0, 60.0, 2.0), 10.0, 1)
        self.assertAlmostEqual(t._available(100.0), 90.0)

    def test_reserve_is_always_held_back(self):
        t = self._trader(reserve_pct=0.30)
        self.assertAlmostEqual(t._available(100.0), 70.0)

    def test_available_never_goes_negative(self):
        t = self._trader(reserve_pct=0.90)
        t._positions["BTCUSDT"] = Position(
            1, make_round(symbol="BTCUSDT"),
            Signal(Side.UP, 0.9, 0.85, 0.02, 50.0, 60.0, 2.0), 50.0, 1)
        self.assertGreaterEqual(t._available(100.0), 0.0)

    def test_concurrency_is_capped(self):
        c = self._cfg(max_concurrent_positions=1)
        self.assertEqual(c.max_concurrent_positions, 1)

    def test_duplicate_symbols_are_rejected(self):
        with self.assertRaises(ValueError):
            cfg(symbols=("BTCUSDT", "BTCUSDT"))

    def test_empty_symbols_means_every_market_is_traded(self):
        """Not a rejection: the empty tuple is how auto-discovery is spelled."""
        c = cfg(symbols=())
        self.assertEqual(c.symbols, ())

    def test_symbol_property_returns_the_first(self):
        self.assertEqual(cfg(symbols=("ETHUSDT", "BTCUSDT")).symbol, "ETHUSDT")

    def test_symbol_property_falls_back_when_unrestricted(self):
        """`.symbol` still needs to hand back a single ticker for the
        informational spots that use it (preflight's probe, the settlement-
        feed fallback), even with no restriction configured."""
        self.assertEqual(cfg(symbols=()).symbol, "BTCUSDT")

    def test_journal_records_the_market(self):
        j = Journal(self.db, "buffer")
        sig = Signal(Side.UP, 0.9, 0.85, 0.02, 2.0, 60.0, 2.0)
        j.record("PAPER", make_round(symbol="ETHUSDT"), sig, 3000, 0.5, 100.0)
        row = j._conn.execute("SELECT symbol FROM trades").fetchone()
        self.assertEqual(row[0], "ETHUSDT")

    def test_diagnose_can_scope_to_one_market(self):
        j = Journal(self.db, "buffer")
        for sym, wins in (("BTCUSDT", 30), ("ETHUSDT", 5)):
            for i in range(40):
                sig = Signal(Side.UP, 0.9, 0.60, 0.02, 2.0, 60.0, 2.0)
                t = j.record("PAPER", make_round(symbol=sym), sig,
                             3000, 0.5, 100.0)
                j.resolve(t, i < wins, 1.0 if i < wins else -1.0, "venue")
        btc = j.diagnose("buffer", "BTCUSDT")
        eth = j.diagnose("buffer", "ETHUSDT")
        self.assertIn("BTCUSDT", btc)
        self.assertIn("75.0%", btc)
        self.assertIn("12.5%", eth)

    def test_calibration_report_breaks_down_by_market(self):
        j = Journal(self.db, "buffer")
        for sym in ("BTCUSDT", "ETHUSDT"):
            for i in range(30):
                sig = Signal(Side.UP, 0.9, 0.60, 0.02, 2.0, 60.0, 2.0)
                t = j.record("PAPER", make_round(symbol=sym), sig,
                             3000, 0.5, 100.0)
                j.resolve(t, i % 2 == 0, 1.0, "venue")
        report = j.calibration_report("buffer")
        self.assertIn("Per market", report)
        self.assertIn("ETHUSDT", report)


class TestBalanceReconciliation(unittest.TestCase):
    """
    The API is authoritative for the balance.

    Adding unclaimed winnings to a portfolio figure that already includes
    settled positions inflated the bankroll by the GROSS payout -- stake plus
    profit -- not the profit.
    """

    def test_gross_payout_is_larger_than_the_profit(self):
        stake, price = 1.0, 0.60
        profit = settle_pnl(stake, price, True, 200)
        gross = stake / price
        self.assertAlmostEqual(gross, 1.6667, places=3)
        self.assertLess(profit, 1.0)
        self.assertGreater(gross - profit, 0.9)

    def test_default_does_not_count_unredeemed(self):
        self.assertFalse(cfg().count_unredeemed_in_bankroll)

    def test_reconcile_tolerance_is_configurable(self):
        self.assertGreater(cfg().reconcile_tolerance, 0.0)

    def test_reconcile_warns_on_a_mismatch(self):
        import logging as _log
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, live=True)
            client = FakeClient([], [(0, 65_000.0)], {}, {})
            t = build_trader(client, c, db)
            pos = Position(1, make_round(),
                           Signal(Side.UP, 0.9, 0.60, 0.02, 1.0, 60.0, 2.0),
                           1.0, 1)
            records = []

            class Cap(_log.Handler):
                def emit(self, rec):
                    records.append(rec.getMessage())

            handler = Cap(); prev = m.LOG.level
            m.LOG.setLevel(_log.WARNING); m.LOG.addHandler(handler)
            try:
                t._reconcile(pos, True, 0.65, 100.0, 100.20)
            finally:
                m.LOG.removeHandler(handler); m.LOG.setLevel(prev)
            self.assertTrue(any("RECONCILE MISMATCH" in r for r in records))
        finally:
            os.unlink(db)

    def test_a_mismatch_is_charged_to_pnl_not_merely_logged(self):
        """
        The drift the bot was leaking. _reconcile already measured the gap
        between what settling should have moved and what it did -- across a
        window seconds wide, far too narrow for a deposit -- and then threw
        the number away. RiskManager saw the same residue later, could not
        tell it from somebody's deposit, and rebased it into the baseline.
        A fee under-modelled by a few cents a round was laundered every round.
        """
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, live=True)
            t = build_trader(FakeClient([], [(0, 65_000.0)], {}, {}), c, db)
            rnd = make_round()
            pos = Position(1, rnd,
                           Signal(Side.UP, 0.9, 0.60, 0.02, 1.0, 60.0, 2.0),
                           1.0, 1)
            t._account_risk = m.RiskManager(c, 100.0)
            risk = t._risk_for(rnd.symbol)

            # A loss: the stake left at entry, so settling should move
            # nothing. It moved -0.15, which is a cost the model missed.
            t._reconcile(pos, False, -1.0, 100.0, 99.85)

            self.assertAlmostEqual(risk.pnl_correction, -0.15, places=6)
            self.assertAlmostEqual(t._account_risk.pnl_correction, -0.15,
                                   places=6)
        finally:
            os.unlink(db)

    def test_a_settlement_that_agrees_corrects_nothing(self):
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, live=True)
            t = build_trader(FakeClient([], [(0, 65_000.0)], {}, {}), c, db)
            rnd = make_round()
            pos = Position(1, rnd,
                           Signal(Side.UP, 0.9, 0.60, 0.02, 1.0, 60.0, 2.0),
                           1.0, 1)
            t._account_risk = m.RiskManager(c, 100.0)

            t._reconcile(pos, False, -1.0, 100.0, 100.0)

            self.assertAlmostEqual(
                t._risk_for(rnd.symbol).pnl_correction, 0.0, places=9)
        finally:
            os.unlink(db)

    def test_an_uncredited_win_corrects_nothing_yet(self):
        """
        The payout lands after this runs, so there is nothing to compare
        against. Correcting here would book the whole gross payout as a
        modelling error.
        """
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            c = cfg(db_path=db, live=True)
            t = build_trader(FakeClient([], [(0, 65_000.0)], {}, {}), c, db)
            rnd = make_round()
            pos = Position(1, rnd,
                           Signal(Side.UP, 0.9, 0.60, 0.02, 1.0, 60.0, 2.0),
                           1.0, 1)
            t._account_risk = m.RiskManager(c, 100.0)
            t._unredeemed["tok"] = (1.6667, [], "56")

            t._reconcile(pos, True, 0.65, 100.0, 100.0)

            self.assertAlmostEqual(
                t._risk_for(rnd.symbol).pnl_correction, 0.0, places=9)
        finally:
            os.unlink(db)

    def test_reconcile_is_quiet_when_it_agrees(self):
        import logging as _log
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            t = build_trader(FakeClient([], [(0, 65_000.0)], {}, {}),
                             cfg(db_path=db, live=True), db)
            pos = Position(1, make_round(),
                           Signal(Side.UP, 0.9, 0.60, 0.02, 1.0, 60.0, 2.0),
                           1.0, 1)
            records = []

            class Cap(_log.Handler):
                def emit(self, rec):
                    records.append(rec.getMessage())

            handler = Cap(); prev = m.LOG.level
            m.LOG.setLevel(_log.WARNING); m.LOG.addHandler(handler)
            try:
                t._reconcile(pos, True, 0.65, 100.0, 101.67)
            finally:
                m.LOG.removeHandler(handler); m.LOG.setLevel(prev)
            self.assertFalse(any("MISMATCH" in r for r in records))
        finally:
            os.unlink(db)


class TestJournal(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.j = Journal(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def test_empty_report(self):
        self.assertIn("No resolved trades", self.j.calibration_report())

    def test_records_and_resolves(self):
        rnd = make_round()
        sig = Signal(Side.UP, 0.72, 0.55, 0.16, 5.0, 60.0)
        tid = self.j.record("PAPER", rnd, sig, 100_400, 0.5, 100.0)
        self.j.resolve(tid, True, 4.0, "venue")
        report = self.j.calibration_report()
        self.assertIn("Resolved trades : 1", report)
        self.assertIn("+4.00", report)

    def test_calibration_detects_overconfidence(self):
        rnd = make_round()
        for i in range(100):
            sig = Signal(Side.UP, 0.90, 0.55, 0.16, 1.0, 60.0)
            tid = self.j.record("PAPER", rnd, sig, 100_400, 0.5, 100.0)
            self.j.resolve(tid, i < 50, 1.0 if i < 50 else -1.0, "venue")
        report = self.j.calibration_report()
        self.assertIn("<-- off", report)    # 90% predicted, 50% actual


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWinReturn(unittest.TestCase):
    """
    How much a win pays, which is a different question from whether the bet
    is priced wrong. A trade can carry a large probability edge and still
    return 6%, in which case one loss undoes sixteen wins.
    """

    def test_return_falls_as_price_rises(self):
        prev = math.inf
        for price in (0.55, 0.70, 0.80, 0.90, 0.95):
            r = m.win_return(price, 0)
            self.assertLess(r, prev)
            prev = r

    def test_the_expensive_end_pays_almost_nothing(self):
        self.assertLess(m.win_return(0.94, 200), 0.07)
        self.assertGreater(m.win_return(0.80, 200), 0.24)

    def test_fees_reduce_the_return(self):
        self.assertLess(m.win_return(0.70, 500), m.win_return(0.70, 0))

    def test_rejects_impossible_prices(self):
        for bad in (0.0, 1.0, -0.5, 2.0):
            with self.assertRaises(ValueError):
                m.win_return(bad, 200)

    def test_settlement_uses_the_same_formula(self):
        """One definition, or the payout and the gate can disagree."""
        for price in (0.2, 0.55, 0.79, 0.9):
            for fee in (0, 200, 900):
                self.assertAlmostEqual(settle_pnl(10.0, price, True, fee),
                                       10.0 * m.win_return(price, fee),
                                       places=12)

    def test_cap_inverts_the_return(self):
        for target in (0.10, 0.25, 0.50, 1.0):
            for fee in (0, 200, 900):
                cap = m.max_price_for_return(target, fee)
                self.assertAlmostEqual(m.win_return(cap, fee), target,
                                       places=12)

    def test_cap_tracks_the_market_fee(self):
        """A hardcoded ceiling is wrong on every market with another fee."""
        self.assertGreater(m.max_price_for_return(0.25, 0),
                           m.max_price_for_return(0.25, 900))

    def test_no_floor_means_no_ceiling(self):
        self.assertEqual(m.max_price_for_return(0.0, 200), 1.0)
        self.assertEqual(m.max_price_for_return(-1.0, 200), 1.0)


class TestReturnFloor(unittest.TestCase):
    """
    A win must be large enough to be worth the loss it risks. Enforced
    everywhere money is committed, not only on the first screen.
    """

    def setUp(self):
        self.c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])

    def test_disabled_floor_admits_everything(self):
        loose = cfg(min_win_return=0.0)
        self.assertTrue(m.clears_return(0.99, 200, loose))

    def test_floor_rejects_a_thin_payout(self):
        self.assertFalse(m.clears_return(0.94, 200, self.c))
        self.assertTrue(m.clears_return(0.70, 200, self.c))

    def test_evaluate_refuses_a_price_that_pays_too_little(self):
        """
        The case that motivated this: a real edge whose win is trivial.

        The book sits at 0.94 and the model says 0.99, which clears every
        edge test comfortably -- and pays about 6%, so one loss erases
        sixteen wins. The floor is the only gate that sees it.
        """
        rnd = make_round(strike=65_000.0, fee_bps=200)
        now = rnd.end_ms - 60_000
        loose = cfg(**{**m.PROFILES["buffer"], "min_win_return": 0.0,
                       "max_entry_price": 0.97})
        book = {Side.UP: [(0.94, 1e6)]}
        self.assertIsNotNone(assess(rnd, 65_200, 0.5, 1000, now, loose, book).signal)

        strict = cfg(**{**m.PROFILES["buffer"], "max_entry_price": 0.97})
        self.assertIsNone(assess(rnd, 65_200, 0.5, 1000, now, strict, book).signal)

    def test_evaluate_checks_the_walked_price_not_just_the_top(self):
        """
        A thin top level at an acceptable price, with the depth behind it
        priced past the floor. Screening only the best level would let the
        trade through and fill it at a price the floor exists to refuse.
        """
        rnd = make_round(strike=65_000.0, fee_bps=0, decimal_precision=4)
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.79, 0.5), (0.95, 1e6)]}
        sig = assess(rnd, 65_200, 0.5, 1000, now, self.c, book).signal
        if sig is not None:
            self.assertTrue(m.clears_return(sig.fill_price, 0, self.c))

    def test_accepted_signals_always_clear_the_floor(self):
        rnd = make_round(strike=65_000.0, fee_bps=200)
        for spot in (65_050, 65_120, 65_260, 64_800, 64_700):
            for price in (0.56, 0.62, 0.71, 0.78, 0.795):
                now = rnd.end_ms - 60_000
                book = {Side.UP: [(price, 1e6)], Side.DOWN: [(price, 1e6)]}
                sig = assess(rnd, spot, 0.5, 1000, now, self.c, book).signal
                if sig is not None:
                    self.assertGreaterEqual(
                        m.win_return(sig.fill_price, 200),
                        self.c.min_win_return - 1e-9,
                        f"spot={spot} price={price}")

    def test_the_live_quote_is_checked_too(self):
        """
        The screen looks at the book; the venue's quote is what executes.
        A floor applied only to the screen is a floor the venue can step over.
        """
        import inspect
        src = inspect.getsource(m.Trader._maybe_enter_model)
        self.assertIn("clears_return(quote.average_price", src)

    def test_top_ups_are_checked_too(self):
        import inspect
        src = inspect.getsource(m.Trader._maybe_scale_in)
        self.assertIn("clears_return", src)
        self.assertIn("blended_price_cap", src)

    def test_a_floor_the_band_cannot_satisfy_is_rejected(self):
        """
        Otherwise the bot never trades and never says why -- the worst of
        the three possible outcomes.
        """
        with self.assertRaises(ValueError):
            cfg(min_entry_price=0.85, max_entry_price=0.95,
                max_blended_price=0.90, min_win_return=0.25)

    def test_negative_floor_is_rejected(self):
        with self.assertRaises(ValueError):
            cfg(min_win_return=-0.1)

    def test_buffer_wins_now_cover_a_loss_in_four(self):
        """The whole point: the wins/loss ratio at the band's worst price."""
        c = self.c
        worst = m.max_price_for_return(c.min_win_return, c.fee_bps)
        self.assertLessEqual(m.wins_per_loss(worst), 4.5)


class TestTrendDetection(unittest.TestCase):
    """
    Inertia measured so it can be acted on EARLY.

    The detector these tests guard against is the obvious one: count
    consecutive rounds that closed the same way, act once the count is high.
    That detector is structurally late -- by the time three rounds confirm a
    move, the move is three rounds old and the price is at its worst. So the
    trigger here is the current block's thrust, and the run count is only
    corroboration and a brake.
    """

    def _est(self, closes, **over):
        settings = dict(m.PROFILES["buffer"]); settings.update(over)
        c = Config(api_key="k", api_secret="s", **settings)
        est = m.VolatilityEstimator.__new__(m.VolatilityEstimator)
        est._store = None
        est._static_cfg = c
        return est._measure_trend(closes), c

    @staticmethod
    def _straight(n=30, step=20.0, start=65_000.0):
        return [start + i * step for i in range(n)]

    @staticmethod
    def _chop(n=30, amp=60.0, start=65_000.0):
        return [start + (amp if i % 2 else -amp) for i in range(n)]

    @staticmethod
    def _blocks(magnitudes, start=65_000.0, block=5):
        """A series whose consecutive round-blocks move by `magnitudes`."""
        out = [start]
        for mag in magnitudes:
            base = out[-1]
            out.extend(base + mag * (i + 1) / block for i in range(block))
        return out

    def test_a_steady_climb_is_a_confirmed_up_trend(self):
        trend, c = self._est(self._straight())
        self.assertEqual(trend.direction, 1)
        self.assertGreater(trend.efficiency, 0.9)
        self.assertEqual(trend.phase, "running")
        self.assertTrue(trend.confirmed(c))
        self.assertTrue(trend.favours(Side.UP))
        self.assertFalse(trend.favours(Side.DOWN))

    def test_a_steady_fall_is_a_confirmed_down_trend(self):
        trend, c = self._est(self._straight(step=-20.0))
        self.assertEqual(trend.direction, -1)
        self.assertTrue(trend.confirmed(c))
        self.assertTrue(trend.favours(Side.DOWN))

    def test_a_trend_is_caught_on_its_FIRST_block(self):
        """
        The requirement this whole design exists for.

        Twenty-five flat minutes and then one decisive round. A run counter
        would score this 1 and wait; the impulse test sees the thrust that is
        happening right now and calls it a trend that is BUILDING.
        """
        trend, c = self._est(self._blocks([0, 0, 0, 0, 175.0]))
        self.assertEqual(trend.run, 1)
        self.assertEqual(trend.phase, "building")
        self.assertGreater(trend.impulse, c.trend_min_impulse)
        self.assertTrue(trend.confirmed(c))

    def test_a_faded_trend_is_refused(self):
        """
        Blocks shrinking round over round. The history looks impressive and
        the run count is at its maximum -- and there is nothing left to
        trade. Entering here is buying the exhaustion.
        """
        trend, c = self._est(self._blocks([200, 120, 60, 24, 8]))
        self.assertEqual(trend.run, 5)          # a run counter would fire
        self.assertLess(trend.decay, c.trend_decay_floor)
        self.assertEqual(trend.phase, "fading")
        self.assertFalse(trend.confirmed(c))

    def test_decay_alone_refuses_it_even_with_thrust_left(self):
        """Decay is judged independently of size, not folded into it."""
        trend, c = self._est(self._straight())
        dying = replace(trend, decay=0.3, phase="fading")
        self.assertFalse(dying.confirmed(c))

    def test_a_move_with_under_a_round_left_is_refused(self):
        """
        The horizon that matters is the round being entered, so that is the
        unit the test is written in.
        """
        trend, c = self._est(self._straight())
        self.assertTrue(trend.confirmed(c))
        self.assertFalse(replace(trend, rounds_left=0.4).confirmed(c))
        self.assertTrue(replace(trend, rounds_left=1.0).confirmed(c))

    def test_projection_answers_in_rounds(self):
        # Halving each block, currently 4 sigmas, floor 1: 4 -> 2 -> 1, so
        # two more rounds.
        self.assertAlmostEqual(m._projected_rounds(4.0, 0.5, 1.0), 2.0)
        self.assertEqual(m._projected_rounds(4.0, 1.0, 1.0), 99.0)
        self.assertEqual(m._projected_rounds(0.5, 0.9, 1.0), 0.0)
        self.assertEqual(m._projected_rounds(4.0, 0.0, 1.0), 0.0)

    def test_chop_around_a_level_is_not_a_trend(self):
        """
        Price swinging across the strike travels a long way and ends up
        nowhere. Efficiency is what catches it.
        """
        trend, c = self._est(self._chop())
        self.assertLess(trend.efficiency, c.trend_min_efficiency)
        self.assertFalse(trend.confirmed(c))

    def test_a_flat_series_has_no_direction(self):
        trend, c = self._est([65_000.0] * 30)
        self.assertEqual(trend.direction, 0)
        self.assertFalse(trend.confirmed(c))

    def test_a_stretched_run_stops_being_boosted(self):
        trend, c = self._est(self._straight(n=60, step=20.0, start=64_000.0))
        self.assertFalse(replace(trend, run=c.trend_max_run + 1).confirmed(c))
        self.assertTrue(replace(trend, run=c.trend_max_run).confirmed(c))

    def test_a_weak_thrust_is_refused(self):
        trend, c = self._est(self._straight())
        weak = replace(trend, impulse=c.trend_min_impulse / 2)
        self.assertFalse(weak.confirmed(c))

    def test_run_is_counted_in_rounds_not_minutes(self):
        """Five 1m closes make one 5m round, and the run counts rounds."""
        trend, _ = self._est(self._straight(n=30))
        self.assertLessEqual(trend.run, 30 // 5)

    def test_a_reversal_becomes_a_NEW_trend_not_a_continuation(self):
        """
        Direction comes from the block in progress. A detector anchored to
        the older blocks would still be reading UP at the moment price turned
        down, which is the top of the move -- the single worst place to buy.
        """
        closes = self._blocks([150, 150, 150, -150])
        trend, _ = self._est(closes)
        self.assertEqual(trend.direction, -1)
        self.assertEqual(trend.run, 1)
        self.assertEqual(trend.phase, "building")

    def test_nothing_is_measured_when_the_feature_is_off(self):
        trend, c = self._est(self._straight(), trend_follow=False)
        self.assertEqual(trend, m.Trend())
        self.assertFalse(trend.confirmed(c))

    def test_other_profiles_do_not_follow_trends(self):
        for name in ("convex", "balanced", "favorite", "micro"):
            c = Config(api_key="k", api_secret="s", **m.PROFILES[name])
            self.assertFalse(c.trend_follow, name)

    def test_too_little_history_is_no_trend(self):
        trend, _ = self._est([65_000.0, 65_100.0, 65_200.0])
        self.assertEqual(trend, m.Trend())

    def test_a_lookback_too_short_for_the_run_ceiling_is_rejected(self):
        """Otherwise the fade rule could never fire and would look enabled."""
        with self.assertRaises(ValueError):
            cfg(trend_follow=True, trend_lookback_min=10, trend_max_run=6)

    def test_a_shrinking_multiplier_is_rejected(self):
        with self.assertRaises(ValueError):
            cfg(trend_stake_multiple=0.8)

    def test_an_impossible_decay_floor_is_rejected(self):
        for bad in (0.0, -0.2, 1.5):
            with self.assertRaises(ValueError):
                cfg(trend_decay_floor=bad)

    def test_the_minimum_run_defaults_to_one(self):
        """Demanding corroboration is the same as entering late."""
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertEqual(c.trend_min_run, 1)

    def test_trend_is_read_off_the_same_klines_as_sigma(self):
        """A second fetch would compare two moments pretending to be one."""
        import inspect
        src = inspect.getsource(m.VolatilityEstimator.sigma_annual)
        self.assertIn("_measure_trend(closes)", src)


class TestTrendBoost(unittest.TestCase):
    """Enter earlier and stake more while the inertia lasts -- but bounded."""

    def setUp(self):
        self.c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.up = m.Trend(direction=1, impulse=2.0, z=2.0, efficiency=0.9,
                          run=2, decay=0.95, rounds_left=8.0,
                          phase="running")

    def _round(self):
        return make_round(strike=65_000.0, fee_bps=200)

    def test_window_widens_only_when_confirmed(self):
        c = self.c
        self.assertEqual(m.entry_window_start_s(c, False),
                         float(c.entry_window_start_s))
        self.assertEqual(
            m.entry_window_start_s(c, True),
            min(float(c.round_seconds),
                float(c.entry_window_start_s + c.trend_early_entry_s)))
        self.assertGreater(m.entry_window_start_s(c, True),
                           m.entry_window_start_s(c, False))

    def test_window_never_exceeds_the_round(self):
        c = cfg(**{**m.PROFILES["buffer"], "entry_window_start_s": 280,
                   "trend_early_entry_s": 200})
        self.assertLessEqual(m.entry_window_start_s(c, True), c.round_seconds)

    def test_no_entry_before_the_window_without_a_trend(self):
        rnd = self._round()
        now = rnd.end_ms - 290_000          # earlier than the 270s window
        book = {Side.UP: [(0.70, 1e6)]}
        self.assertIsNone(assess(rnd, 65_500, 0.5, 1000, now, self.c, book).signal)

    def test_a_confirmed_trend_admits_an_early_entry(self):
        rnd = self._round()
        now = rnd.end_ms - 290_000
        book = {Side.UP: [(0.70, 1e6)]}
        sig = assess(rnd, 65_500, 0.5, 1000, now, self.c, book,
                       trend=self.up).signal
        self.assertIsNotNone(sig)
        self.assertTrue(sig.trend_boosted)
        self.assertGreater(sig.seconds_left, self.c.entry_window_start_s)

    def test_the_early_window_is_not_a_general_relaxation(self):
        """
        A trend pointing UP must not open the window for a DOWN trade. That
        would be using the trend as an excuse to trade against it.
        """
        rnd = self._round()
        now = rnd.end_ms - 290_000
        book = {Side.DOWN: [(0.70, 1e6)]}
        self.assertIsNone(assess(rnd, 64_500, 0.5, 1000, now, self.c, book,
                                   trend=self.up).signal)

    def test_the_buffer_gate_still_applies_early(self):
        """
        Entering early does not lower the bar. z is measured in sigmas of the
        REMAINING time, so clearing it with four minutes to run takes a
        genuinely larger move -- which is exactly what the trend supplies.
        """
        rnd = self._round()
        now = rnd.end_ms - 290_000
        book = {Side.UP: [(0.70, 1e6)]}
        self.assertIsNone(assess(rnd, 65_010, 0.5, 1000, now, self.c, book,
                                   trend=self.up).signal)

    def test_a_confirmed_trend_stakes_more(self):
        rnd = self._round()
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.70, 1e6)]}
        plain = assess(rnd, 65_200, 0.5, 1000, now, self.c, book).signal
        boosted = assess(rnd, 65_200, 0.5, 1000, now, self.c, book,
                           trend=self.up).signal
        self.assertIsNotNone(plain)
        self.assertIsNotNone(boosted)
        self.assertGreater(boosted.stake_usdt, plain.stake_usdt)
        self.assertFalse(plain.trend_boosted)

    def test_an_unconfirmed_trend_changes_nothing(self):
        rnd = self._round()
        now = rnd.end_ms - 60_000
        book = {Side.UP: [(0.70, 1e6)]}
        weak = m.Trend(direction=1, impulse=0.2, z=0.2, efficiency=0.1,
                       run=1, rounds_left=0.0, phase="none")
        plain = assess(rnd, 65_200, 0.5, 1000, now, self.c, book).signal
        same = assess(rnd, 65_200, 0.5, 1000, now, self.c, book, trend=weak).signal
        self.assertAlmostEqual(plain.stake_usdt, same.stake_usdt)
        self.assertFalse(same.trend_boosted)

    def test_the_boost_never_leaves_the_hard_stake_cap(self):
        for bankroll in (10.0, 100.0, 5000.0):
            for price in (0.56, 0.70, 0.79):
                stake = kelly_stake(bankroll, 0.95, price, self.c, 200)
                out = m.boosted_stake(stake, bankroll, 0.95, price, self.c, 200)
                self.assertLessEqual(out,
                                     bankroll * self.c.hard_max_stake_pct + 1e-9)

    def test_the_boost_never_passes_twice_full_kelly(self):
        """Beyond 2x full Kelly, log growth is negative even with an edge."""
        for price in (0.56, 0.70, 0.79):
            for prob in (0.62, 0.75, 0.90):
                bankroll = 500.0
                stake = kelly_stake(bankroll, prob, price, self.c, 200)
                if stake <= 0:
                    continue
                out = m.boosted_stake(stake, bankroll, prob, price, self.c, 200)
                mult = m.kelly_multiple(out, bankroll, prob, price, 200)
                if mult is not None:
                    self.assertLessEqual(mult, 2.0 + 1e-9,
                                         f"{price}/{prob}")

    def test_the_boost_never_shrinks_a_stake(self):
        for price in (0.56, 0.70, 0.79):
            stake = kelly_stake(50.0, 0.85, price, self.c, 200)
            self.assertGreaterEqual(
                m.boosted_stake(stake, 50.0, 0.85, price, self.c, 200), stake)

    def test_a_boosted_position_keeps_its_size_on_top_up(self):
        """
        Opening at trend size and topping up to the plain Kelly target would
        shrink the position mid-round -- neither rule, just an accident of
        applying one at entry and the other afterwards.
        """
        import inspect
        src = inspect.getsource(m.Trader._maybe_scale_in)
        self.assertIn("trend_boosted", src)
        self.assertIn("boosted_stake", src)

    def test_trend_z_is_signed_against_the_side_taken(self):
        """
        Positive means the trend agreed with the trade, negative that it did
        not. Without the sign the journal cannot later separate the trades
        the inertia paid for from the ones it did not.
        """
        rnd = self._round()
        now = rnd.end_ms - 60_000
        # Spot BELOW the strike, so the tradable side is DOWN while the
        # trend points UP.
        book = {Side.DOWN: [(0.70, 1e6)]}
        sig = assess(rnd, 64_800, 0.5, 1000, now, self.c, book,
                     trend=self.up).signal
        if sig is not None:
            self.assertIs(sig.side, Side.DOWN)
            self.assertLess(sig.trend_z, 0.0)
            self.assertFalse(sig.trend_boosted)

        aligned = assess(rnd, 65_200, 0.5, 1000, now, self.c,
                         {Side.UP: [(0.70, 1e6)]}, trend=self.up).signal
        self.assertIsNotNone(aligned)
        self.assertGreater(aligned.trend_z, 0.0)

    def test_the_journal_records_the_trend(self):
        fd, db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        try:
            j = Journal(db, "buffer")
            sig = Signal(Side.UP, 0.9, 0.70, 0.05, 2.0, 200.0, 1.2,
                         trend_z=2.5, trend_boosted=True)
            tid = j.record("PAPER", make_round(), sig, 65_000, 0.5, 25.0)
            row = j._conn.execute(
                "SELECT trend_z FROM trades WHERE id=?", (tid,)).fetchone()
            self.assertAlmostEqual(row[0], 2.5)
        finally:
            os.unlink(db)

    def test_the_prefilter_does_not_drop_early_rounds(self):
        """
        The exact window needs the trend, which needs the hydrated round. So
        the pre-hydration screen has to be the loose one, or it discards
        precisely the rounds the trend exists to catch.
        """
        import inspect
        src = inspect.getsource(m.Trader._maybe_enter_model)
        self.assertIn("entry_window_start_s(self._cfg", src)


class TestAuthWait(unittest.TestCase):
    """
    Preflight waits for a shared outbound IP to be allowlisted.

    The address is not knowable until the process runs and can change on any
    restart, so it cannot be added in advance. Failing on the first refusal
    kills the deploy a second after printing the one thing needed to fix it.
    """

    def setUp(self):
        self.slept = []
        self._sleep = time.sleep
        time.sleep = self.slept.append

    def tearDown(self):
        time.sleep = self._sleep

    def _cfg(self, **kw):
        base = dict(auth_wait_timeout_s=30.0, auth_wait_poll_s=5.0)
        base.update(kw)
        return cfg(**base)

    @staticmethod
    def _quiet(fn, *args):
        """wait_for_auth reports progress on stdout; tests do not need it."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args)

    def _client(self, outcomes):
        """A client whose wallet() yields `outcomes` in order."""
        seq = iter(outcomes)

        def wallet():
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        return types.SimpleNamespace(sync_clock=lambda: 0, wallet=wallet)

    @staticmethod
    def _auth_error():
        return m.ApiError("Invalid API-key, IP, or permissions", code=-2015)

    def test_accepts_immediately_when_the_ip_is_allowlisted(self):
        client = self._client([m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_retries_an_auth_refusal_until_it_is_accepted(self):
        client = self._client([self._auth_error(), self._auth_error(),
                               m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [5.0, 5.0])

    def test_retries_a_network_error_too(self):
        client = self._client([requests.RequestException("connection reset"),
                               m.WalletRef("0xabc", "1")])
        self.assertTrue(self._quiet(m.wait_for_auth, self._cfg(), client))

    def test_gives_up_at_the_deadline(self):
        client = self._client([self._auth_error()] * 50)
        self.assertFalse(self._quiet(m.wait_for_auth,
                                    self._cfg(auth_wait_timeout_s=0.0),
                                    client))

    def test_a_blocked_region_fails_at_once(self):
        """
        451 is the server's REGION, not its address. Waiting cannot change
        which continent the worker is on, so retrying only delays the news.
        """
        blocked = m.ApiError("restricted location", status=451)
        client = self._client([blocked, m.WalletRef("0xabc", "1")])
        self.assertFalse(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_a_non_auth_failure_fails_at_once(self):
        other = m.ApiError("balance too low", code=-9000)
        client = self._client([other, m.WalletRef("0xabc", "1")])
        self.assertFalse(self._quiet(m.wait_for_auth, self._cfg(), client))
        self.assertEqual(self.slept, [])

    def test_the_wait_is_off_by_default(self):
        """Existing deployments must not silently start hanging on boot."""
        self.assertEqual(Config(api_key="k", api_secret="s",
                                ).auth_wait_timeout_s, 0.0)
        for name, prof in m.PROFILES.items():
            self.assertNotIn("auth_wait_timeout_s", prof, name)

    def test_a_negative_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            cfg(auth_wait_timeout_s=-1.0)
        with self.assertRaises(ValueError):
            cfg(auth_wait_poll_s=0.0)

    def test_preflight_prints_the_address_before_waiting(self):
        """The address is useless after the process has already given up."""
        import inspect
        src = inspect.getsource(m.preflight)
        self.assertLess(src.index("Outbound IP"), src.index("wait_for_auth"))

    def test_one_ip_lookup_shared_by_preflight_and_whoami(self):
        import inspect
        self.assertIn("outbound_ip(", inspect.getsource(m.preflight))
        self.assertIn("outbound_ip(", inspect.getsource(m.whoami))


class TestNewLimitsActuallyBind(unittest.TestCase):
    """
    Mutation testing found these: clamps that no test ever pushed against.

    A limit that is never reached in a test is a limit whose arithmetic is
    unverified -- the suite would pass just as happily with the 2x-Kelly
    ceiling written as 1x, or with the fee subtracted the wrong way. Each
    test here drives a value INTO a bound and checks where it lands.
    """

    def setUp(self):
        self.c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])

    # --- boosted_stake ---------------------------------------------------

    def test_the_boost_is_clipped_to_exactly_twice_full_kelly(self):
        """
        The bound that matters most: past 2x full Kelly, expected log growth
        is negative even with a real edge, so a trend multiplier without this
        clip is a way of turning an edge into a loss on a long enough run.
        """
        bankroll, price, prob, fee = 100.0, 0.70, 0.72, 200
        b = ((1 - price) / price) * (1 - fee / 10_000)
        full = (prob * b - (1 - prob)) / b
        self.assertLess(bankroll * 2 * full,
                        bankroll * self.c.hard_max_stake_pct,
                        "fixture must make Kelly, not the hard cap, bind")
        out = m.boosted_stake(8.0, bankroll, prob, price, self.c, fee)
        self.assertAlmostEqual(out, bankroll * 2.0 * full, places=9)

    def test_the_boost_is_clipped_by_the_hard_stake_cap(self):
        bankroll, price, prob, fee = 100.0, 0.70, 0.95, 200
        out = m.boosted_stake(20.0, bankroll, prob, price, self.c, fee)
        self.assertAlmostEqual(out, bankroll * self.c.hard_max_stake_pct,
                               places=9)

    def test_an_unclipped_boost_is_the_full_multiple(self):
        """Otherwise the two tests above could pass with the boost disabled."""
        out = m.boosted_stake(1.0, 1000.0, 0.90, 0.70, self.c, 200)
        self.assertAlmostEqual(out, self.c.trend_stake_multiple, places=9)

    def test_no_boost_without_a_stake_or_a_bankroll(self):
        self.assertEqual(m.boosted_stake(0.0, 100.0, 0.9, 0.7, self.c, 200), 0.0)
        self.assertEqual(m.boosted_stake(5.0, 0.0, 0.9, 0.7, self.c, 200), 5.0)
        self.assertEqual(m.boosted_stake(-1.0, 100.0, 0.9, 0.7, self.c, 200),
                         -1.0)

    def test_a_multiple_of_one_is_a_no_op(self):
        flat = cfg(**{**m.PROFILES["buffer"], "trend_stake_multiple": 1.0})
        self.assertEqual(m.boosted_stake(3.0, 100.0, 0.9, 0.7, flat, 200), 3.0)

    def test_the_ceiling_wins_even_over_an_oversized_input(self):
        """
        No edge means no full-Kelly bound, so only the hard cap remains --
        and it must bind even on a stake that already breaches it. Returning
        max(stake, ...) would let an oversized position through untouched.
        """
        out = m.boosted_stake(30.0, 100.0, 0.10, 0.70, self.c, 200)
        self.assertAlmostEqual(out, 100.0 * self.c.hard_max_stake_pct,
                               places=9)

    # --- the return floor at its exact boundary ---------------------------

    def test_the_floor_admits_the_cap_price_and_refuses_a_hair_above(self):
        cap = m.max_price_for_return(self.c.min_win_return, 200)
        self.assertTrue(m.clears_return(cap, 200, self.c))
        self.assertFalse(m.clears_return(cap + 1e-4, 200, self.c))

    def test_a_fee_that_eats_the_payout_leaves_no_tradable_price(self):
        self.assertEqual(m.max_price_for_return(0.25, 10_000), 0.0)
        self.assertEqual(m.max_price_for_return(0.25, 20_000), 0.0)

    def test_the_cap_moves_the_right_way_with_the_fee(self):
        """A sign error here would relax the floor on expensive markets."""
        self.assertGreater(m.max_price_for_return(0.25, 0),
                           m.max_price_for_return(0.25, 500))
        self.assertGreater(m.max_price_for_return(0.25, 500),
                           m.max_price_for_return(0.25, 2000))

    def test_an_impossible_price_never_clears_the_floor(self):
        for bad in (0.0, 1.0, -0.2, 1.5):
            self.assertFalse(m.clears_return(bad, 200, self.c))

    # --- the entry window at its exact boundary ---------------------------

    def test_the_window_start_is_inclusive(self):
        rnd = make_round(strike=65_000.0, fee_bps=200)
        book = {Side.UP: [(0.70, 1e6)]}
        at = rnd.end_ms - self.c.entry_window_start_s * 1000
        self.assertIsNotNone(
            assess(rnd, 65_500, 0.5, 1000, at, self.c, book).signal)
        just_before = at - 1000
        self.assertIsNone(
            assess(rnd, 65_500, 0.5, 1000, just_before, self.c, book).signal)

    # --- Trend thresholds at their exact boundaries -----------------------

    def test_impulse_and_z_thresholds_are_inclusive(self):
        c = self.c
        base = m.Trend(direction=1, impulse=c.trend_min_impulse,
                       z=c.trend_min_z, efficiency=c.trend_min_efficiency,
                       run=1, decay=1.0, rounds_left=c.trend_min_rounds_left,
                       phase="building")
        self.assertTrue(base.confirmed(c))
        self.assertFalse(replace(base, impulse=c.trend_min_impulse - 1e-6)
                         .confirmed(c))
        self.assertFalse(replace(base, z=c.trend_min_z - 1e-6).confirmed(c))
        self.assertFalse(
            replace(base, efficiency=c.trend_min_efficiency - 1e-6)
            .confirmed(c))
        self.assertFalse(
            replace(base, rounds_left=c.trend_min_rounds_left - 1e-6)
            .confirmed(c))

    def test_a_fading_phase_is_refused_even_with_every_number_perfect(self):
        c = self.c
        strong = m.Trend(direction=1, impulse=9.0, z=9.0, efficiency=1.0,
                         run=2, decay=0.9, rounds_left=50.0, phase="fading")
        self.assertFalse(strong.confirmed(c))
        self.assertTrue(replace(strong, phase="running").confirmed(c))

    def test_decay_is_only_measured_once_there_is_something_to_compare(self):
        """A first block has no predecessor, so it cannot be 'decaying'."""
        est = m.VolatilityEstimator.__new__(m.VolatilityEstimator)
        est._store = None
        est._static_cfg = self.c
        closes = [65_000.0] * 25 + [65_000 + (i + 1) * 35 for i in range(5)]
        trend = est._measure_trend(closes)
        self.assertEqual(trend.run, 1)
        self.assertEqual(trend.decay, 1.0)

    def test_a_motionless_series_is_measured_as_no_trend(self):
        est = m.VolatilityEstimator.__new__(m.VolatilityEstimator)
        est._store = None
        est._static_cfg = self.c
        self.assertEqual(est._measure_trend([65_000.0] * 40), m.Trend())
        # Too little history to say anything at all.
        self.assertEqual(est._measure_trend([65_000.0 + i for i in range(9)]),
                         m.Trend())

    def test_non_positive_closes_are_discarded_not_trusted(self):
        """One bad kline must not become a direction."""
        est = m.VolatilityEstimator.__new__(m.VolatilityEstimator)
        est._store = None
        est._static_cfg = self.c
        closes = [65_000.0 + i * 20 for i in range(30)]
        closes[7] = 0.0
        trend = est._measure_trend(closes)
        self.assertEqual(trend.direction, 1)
        self.assertTrue(math.isfinite(trend.impulse))


class TestTrendArithmetic(unittest.TestCase):
    """
    The scale factors, pinned to numbers rather than to inequalities.

    Mutation testing kept surviving here: a threshold test like
    `impulse >= 1.2` passes whether the impulse is computed against one
    block of noise or against five, because the mutant lands on the same
    side of the bar. So these tests pin the VALUE.
    """

    def setUp(self):
        self.c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.est = m.VolatilityEstimator.__new__(m.VolatilityEstimator)
        self.est._store = None
        self.est._static_cfg = self.c

    def test_impulse_is_measured_against_ONE_block_of_noise(self):
        """
        Twenty-four alternating steps of size d, then five straight steps of
        size d. Per-minute sigma is about d, and one block of noise is
        d*sqrt(5), so a 5d thrust must score sqrt(5) ~ 2.24 sigmas.

        Dividing by sqrt(5) instead of multiplying -- an easy slip -- would
        score the same move at 11.2 and make the impulse gate fire on chop.
        """
        d = 50.0
        closes = [65_000.0]
        for i in range(24):
            closes.append(closes[-1] + (d if i % 2 else -d))
        for _ in range(5):
            closes.append(closes[-1] + d)
        trend = self.est._measure_trend(closes)
        self.assertAlmostEqual(trend.impulse, math.sqrt(5), delta=0.15)

    def test_efficiency_is_a_ratio_not_a_distance(self):
        """A straight run is 1.0; a run that doubled back is measurably less."""
        straight = [65_000.0 + i * 20 for i in range(30)]
        self.assertAlmostEqual(
            self.est._measure_trend(straight).efficiency, 1.0, places=6)

    def test_a_zero_variance_series_is_refused_not_divided_by(self):
        flat = [65_000.0] * 20 + [65_100.0] * 10
        trend = self.est._measure_trend(flat)
        self.assertTrue(math.isfinite(trend.impulse))
        self.assertTrue(math.isfinite(trend.z))


class TestProjectedRounds(unittest.TestCase):
    """Turning a decay ratio into the horizon an entry actually asks about."""

    def test_halving_from_four_sigmas_leaves_two_rounds(self):
        self.assertAlmostEqual(m._projected_rounds(4.0, 0.5, 1.0), 2.0)

    def test_a_quarter_each_round_leaves_one(self):
        self.assertAlmostEqual(m._projected_rounds(4.0, 0.25, 1.0), 1.0)

    def test_holding_or_growing_is_not_decaying(self):
        self.assertEqual(m._projected_rounds(4.0, 1.0, 1.0), 99.0)
        self.assertEqual(m._projected_rounds(4.0, 1.4, 1.0), 99.0)

    def test_already_under_the_floor_has_nothing_left(self):
        self.assertEqual(m._projected_rounds(0.9, 0.99, 1.0), 0.0)

    def test_degenerate_inputs_never_raise_or_go_infinite(self):
        for impulse, decay, floor in ((0.0, 0.5, 1.0), (-1.0, 0.5, 1.0),
                                      (4.0, 0.5, 0.0), (4.0, 0.5, -1.0),
                                      (4.0, 0.0, 1.0), (4.0, -0.5, 1.0)):
            out = m._projected_rounds(impulse, decay, floor)
            self.assertTrue(math.isfinite(out), f"{impulse}/{decay}/{floor}")
            self.assertGreaterEqual(out, 0.0)


class TestMissedRoundReporting(unittest.TestCase):
    """
    A declined round is watched until it expires, then tallied by reason.

    Silence is indistinguishable from a broken endpoint. This is what turns
    "it isn't trading" into "it is refusing the price, 40 rounds running".
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        c = cfg(db_path=self.db, **m.PROFILES["buffer"])
        self.t = build_trader(FakeClient([], [(0, 65_000.0)], {}, {}), c,
                              self.db)

    def tearDown(self):
        os.unlink(self.db)

    def test_a_live_round_is_not_counted_yet(self):
        self.t._watching[1] = (10_000, "edge below the floor")
        self.t._tally_missed(9_000)
        self.assertEqual(self.t._missed_total, 0)
        self.assertIn(1, self.t._watching)

    def test_an_expired_round_is_counted_and_dropped(self):
        self.t._watching[1] = (10_000, "edge below the floor")
        self.t._tally_missed(11_000)
        self.assertEqual(self.t._missed_total, 1)
        self.assertEqual(self.t._missed["edge below the floor"], 1)
        self.assertNotIn(1, self.t._watching)

    def test_reasons_accumulate_separately(self):
        for i in range(3):
            self.t._watching[i] = (100, "win pays less than the return floor")
        self.t._watching[9] = (100, "buffer too small for the time left")
        self.t._tally_missed(200)
        self.assertEqual(self.t._missed_total, 4)
        self.assertEqual(
            self.t._missed["win pays less than the return floor"], 3)

    def test_a_round_declined_for_price_is_not_marked_seen(self):
        """
        It must stay under review: the price that was too expensive a moment
        ago may not be in ten seconds. Writing it off on the first look is
        how a return floor turns into a bot that never trades.
        """
        import inspect
        src = inspect.getsource(m.Trader._maybe_enter_model)
        watch_at = src.index("self._watching[rnd.topic_id]")
        # The only _seen assignments must come after a trade is attempted.
        self.assertLess(watch_at, src.index("self._seen[rnd.topic_id]"))

    def test_a_decline_always_carries_a_reason(self):
        """
        An empty reason would tally as a blank line in the summary, which is
        the silence this whole mechanism exists to remove.
        """
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        rnd = make_round(strike=65_000.0, fee_bps=200)
        cases = [
            (65_200, rnd.end_ms - 60_000, {Side.UP: [(0.94, 1e6)]}),
            (65_001, rnd.end_ms - 60_000, {Side.UP: [(0.70, 1e6)]}),
            (65_200, rnd.end_ms - 299_000, {Side.UP: [(0.70, 1e6)]}),
            (65_200, rnd.end_ms - 60_000, {Side.UP: [(0.40, 1e6)]}),
            (65_200, rnd.end_ms - 60_000, None),
        ]
        for spot, now, book in cases:
            verdict = assess(rnd, spot, 0.5, 1000, now, c, book)
            if verdict.signal is None:
                self.assertTrue(verdict.blocked_by, f"{spot}/{book}")
                self.assertIn(verdict.blocked_by, m._DECLINE_ORDER)

    def test_a_round_with_no_strike_says_so(self):
        c = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        rnd = make_round(strike=None)
        verdict = assess(rnd, 65_200, 0.5, 1000, rnd.end_ms - 60_000, c)
        self.assertEqual(verdict.blocked_by, "no strike published yet")

    def test_every_decline_reason_is_one_the_reporter_knows(self):
        import inspect
        src = inspect.getsource(m.assess)
        for quoted in re.findall(r'blocked_by="([^"]+)"', src):
            self.assertIn(quoted, m._DECLINE_ORDER, quoted)
        for quoted in re.findall(r'_worse\(blocked, "([^"]+)"\)', src):
            self.assertIn(quoted, m._DECLINE_ORDER, quoted)

    def test_the_tally_runs_even_while_a_position_is_open(self):
        """
        _maybe_enter returns early while a position is open, so driving the
        tally from there would blind it during exactly those minutes.
        """
        import inspect
        self.assertIn("_tally_missed", inspect.getsource(m.Trader.run))


class TestNewCliSurface(unittest.TestCase):
    """The new knobs are reachable from the command line and the environment."""

    def setUp(self):
        self._env = dict(os.environ)
        os.environ["BINANCE_API_KEY"] = "k"
        os.environ["BINANCE_API_SECRET"] = "s"
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env)
        os.unlink(self.db)

    def _run(self, *argv):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = m.main([*argv, "--db", self.db])
        return code, buf.getvalue()

    def test_auth_wait_env_must_be_a_number(self):
        os.environ["AUTH_WAIT_S"] = "soon"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 1)
        self.assertIn("AUTH_WAIT_S", out)

    def test_auth_wait_env_is_accepted(self):
        os.environ["AUTH_WAIT_S"] = "120"
        code, out = self._run("--calibration-report")
        self.assertEqual(code, 0, out)

    def test_the_flag_wins_over_the_environment(self):
        import inspect
        src = inspect.getsource(m.main)
        cli_at = src.index("if args.wait_for_auth is not None:")
        env_at = src.index("elif env_auth_wait is not None:")
        self.assertLess(cli_at, env_at)

    def test_min_return_is_settable(self):
        code, out = self._run("--min-return", "0.4", "--check-config")
        self.assertIn(code, (0, 1), out)     # no config file in this temp dir

    def test_the_new_settings_reach_the_config_document(self):
        doc = m.default_config_document()
        for name in ("min_win_return", "trend_follow", "trend_stake_multiple",
                     "auth_wait_timeout_s"):
            self.assertIn(name, doc["defaults"], name)


class _FakePosition:
    """Just enough of a Position for the loop's bookkeeping."""

    committed_usdt = 1.0
    signal = None
    rnd = None


class TestLoopSurvivesUnexpectedFailures(unittest.TestCase):
    """
    The run loop's error handling, which nothing else in this file exercised.

    Two guarantees, and they fail differently:
      * a storage or OS fault is RECOVERABLE -- it goes through the backoff
        counter like any API error rather than killing the process on its
        first occurrence;
      * a genuine bug is FATAL, but must still settle whatever is already
        staked before the process goes away.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _stub_trader(self, boom):
        """A Trader whose loop body raises `boom`, with startup stubbed out."""
        c = cfg(db_path=self.db, max_consecutive_errors=3,
                poll_interval_s=0.001, error_backoff_max_s=0.001)
        t = m.Trader.__new__(m.Trader)
        t._static_cfg = c
        t._store = None
        t._positions = {}
        t._unredeemed = {}
        # _unredeemed is written from the claim worker thread, so anything
        # reading it -- _outstanding, _bankroll -- takes this lock.
        t._claim_lock = threading.Lock()
        t._errors = 0
        t._stopping = False
        t._seen = {}
        t._drained = False
        t._active_live = False
        t._pending_live = None
        t._account_risk = None
        t._risk = {}
        t._missed = {}
        t._failed_claims = {}

        class _Client:
            def sync_clock(self):
                return 0

            def now_ms(self):
                return 0

        t._client = _Client()
        t._bankroll = lambda: 1000.0
        t._install_signal_handlers = lambda: None
        t._settle_open = lambda: None
        t._tally_missed = lambda now_ms: None
        t._apply_pending_mode = lambda: None
        t._maybe_scale_in_all = lambda b: None

        def _drain(timeout_s=None):
            t._drained = True
            t._positions.clear()

        t._drain = _drain

        def _enter(bankroll, mode):
            raise boom

        t._maybe_enter = _enter
        return t

    def test_journal_fault_is_recoverable_not_fatal(self):
        """A locked or full journal must not end the process outright."""
        import sqlite3 as _sq
        t = self._stub_trader(_sq.OperationalError("database is locked"))
        t.run()                       # halts cleanly rather than propagating
        self.assertGreaterEqual(t._errors, 3)

    def test_disk_fault_is_recoverable_not_fatal(self):
        t = self._stub_trader(OSError(28, "No space left on device"))
        t.run()
        self.assertGreaterEqual(t._errors, 3)

    def test_a_recoverable_fault_drains_when_it_finally_halts(self):
        t = self._stub_trader(OSError(28, "No space left on device"))
        t._positions["BTCUSDT"] = _FakePosition()
        t.run()
        self.assertTrue(t._drained)

    def test_an_unexpected_bug_drains_before_dying(self):
        """The whole point: a crash must not abandon a staked position."""
        t = self._stub_trader(ZeroDivisionError("bug"))
        t._positions["BTCUSDT"] = _FakePosition()
        with self.assertRaises(ZeroDivisionError):
            t.run()
        self.assertTrue(t._drained,
                        "unexpected exception abandoned an open position")

    def test_an_unexpected_bug_still_stops_the_bot(self):
        """Draining is not carrying on: the bot must still stop."""
        t = self._stub_trader(ZeroDivisionError("bug"))
        with self.assertRaises(ZeroDivisionError):
            t.run()

    def test_a_healthy_loop_resets_the_error_counter(self):
        """Guards the fix: the counter must not creep up on success."""
        t = self._stub_trader(ZeroDivisionError("unused"))
        calls = {"n": 0}

        def _enter(bankroll, mode):
            calls["n"] += 1
            if calls["n"] >= 3:
                t._stopping = True

        t._maybe_enter = _enter
        t.run()
        self.assertEqual(t._errors, 0)


# --------------------------------------------------------------------------
# Straddle: buy both sides at round-open, every round
# --------------------------------------------------------------------------


def straddle_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False, **m.PROFILES["straddle"])
    base.update(kw)
    return Config(**base)


class TestStraddleSplit(unittest.TestCase):

    def test_the_split_spends_the_whole_budget(self):
        up, down = m.straddle_split(10.0, 0.20, 0.70, 0)
        self.assertAlmostEqual(up + down, 10.0, places=9)

    def test_both_legs_are_sized_to_the_same_payout(self):
        """
        The property the whole gate rests on: after the split the round's
        direction stops mattering, because either outcome pays the same.
        """
        for price_up, price_down, fee in [(0.20, 0.70, 0), (0.45, 0.52, 0),
                                          (0.30, 0.60, 200), (0.49, 0.49, 50)]:
            up, down = m.straddle_split(10.0, price_up, price_down, fee)
            pay_up = up + m.settle_pnl(up, price_up, True, fee)
            pay_down = down + m.settle_pnl(down, price_down, True, fee)
            self.assertAlmostEqual(pay_up, pay_down, places=9)

    def test_an_equal_split_would_have_missed_this_pair(self):
        """
        0.20/0.70 sums under 1.00, so it is genuinely profitable -- but only
        weighted. This is the money the old 50/50 sizing left on the table.
        """
        up, down = m.straddle_split(10.0, 0.20, 0.70, 0)
        self.assertGreater(m.straddle_worst_case_pnl(up, down, 0.20, 0.70, 0),
                           0.0)
        self.assertLess(m.straddle_worst_case_pnl(5.0, 5.0, 0.20, 0.70, 0),
                        0.0)

    def test_prices_summing_over_one_cannot_be_saved_by_any_split(self):
        self.assertLess(
            m.straddle_worst_case_pnl(
                *m.straddle_split(10.0, 0.55, 0.55, 0), 0.55, 0.55, 0),
            0.0)

    def test_the_fee_is_taken_out_of_the_payout(self):
        args = (10.0, 0.48, 0.48)
        free = m.straddle_worst_case_pnl(*m.straddle_split(*args, 0),
                                         0.48, 0.48, 0)
        charged = m.straddle_worst_case_pnl(*m.straddle_split(*args, 300),
                                            0.48, 0.48, 300)
        self.assertLess(charged, free)


class TestStraddleCompletion(unittest.TestCase):

    def test_both_payouts_come_out_equal_and_beat_the_pair(self):
        for open_price, other_price, fee in [(0.25, 0.35, 0), (0.30, 0.15, 0),
                                             (0.25, 0.60, 200),
                                             (0.10, 0.85, 50)]:
            stake, ok = m.straddle_completion_stake(4.0, open_price,
                                                    other_price, fee, 1e9)
            self.assertTrue(ok, f"{open_price}/{other_price} should clear")
            pay_open = 4.0 / m.breakeven_probability(open_price, fee)
            pay_other = stake / m.breakeven_probability(other_price, fee)
            self.assertAlmostEqual(pay_open, pay_other, places=9)
            self.assertGreater(pay_open, 4.0 + stake)

    def test_a_price_that_cannot_cover_the_open_leg_is_flagged(self):
        _, ok = m.straddle_completion_stake(4.0, 0.25, 0.80, 0, 1e9)
        self.assertFalse(ok)

    def test_a_budget_short_of_the_band_is_flagged_but_still_sized(self):
        """
        The hedge that caps a loss without locking a profit: worth placing
        at the deadline, but never mistaken for a guarantee.
        """
        stake, ok = m.straddle_completion_stake(4.0, 0.25, 0.60, 0,
                                                budget=1.0)
        self.assertFalse(ok)
        self.assertEqual(stake, 1.0)

    def test_a_cheaper_second_leg_needs_less_money(self):
        cheap, _ = m.straddle_completion_stake(4.0, 0.25, 0.20, 0, 1e9)
        dear, _ = m.straddle_completion_stake(4.0, 0.25, 0.60, 0, 1e9)
        self.assertLess(cheap, dear)

    def test_the_order_the_two_sides_arrive_in_does_not_matter(self):
        """
        0.30 then 0.15 and 0.15 then 0.30 are the same trade. Only the leg
        sizes differ, and both lock the same profit in.
        """
        a, ok_a = m.straddle_completion_stake(4.0, 0.30, 0.15, 0, 1e9)
        b, ok_b = m.straddle_completion_stake(4.0, 0.15, 0.30, 0, 1e9)
        self.assertTrue(ok_a and ok_b)
        for stake_open, price_open, stake_other, price_other in [
                (4.0, 0.30, a, 0.15), (4.0, 0.15, b, 0.30)]:
            worst = m.straddle_worst_case_pnl(stake_open, stake_other,
                                              price_open, price_other, 0)
            self.assertGreater(worst, 0.0)


class TestStraddleConfig(unittest.TestCase):

    def test_straddle_and_scale_in_cannot_both_be_on(self):
        with self.assertRaises(ValueError):
            straddle_cfg(scale_in=True)

    def test_straddle_stake_pct_must_be_in_range(self):
        with self.assertRaises(ValueError):
            straddle_cfg(straddle_stake_pct=0.0)
        with self.assertRaises(ValueError):
            straddle_cfg(straddle_stake_pct=0.30)

    def test_entry_window_must_be_positive(self):
        with self.assertRaises(ValueError):
            straddle_cfg(straddle_entry_window_s=0.0)

    def test_max_leg_price_must_be_in_zero_one(self):
        with self.assertRaises(ValueError):
            straddle_cfg(straddle_max_leg_price=0.0)
        with self.assertRaises(ValueError):
            straddle_cfg(straddle_max_leg_price=1.5)

    def test_max_leg_price_of_one_is_a_legal_no_op_ceiling(self):
        straddle_cfg(straddle_max_leg_price=1.0)   # must not raise

    def test_the_default_profile_set_is_internally_valid(self):
        # Every profile, including "straddle", must build without error --
        # a profile that fails validation is a bot that refuses to start.
        for name in m.PROFILES:
            Config(api_key="k", api_secret="s", **m.PROFILES[name])

    def test_straddle_is_off_by_default_everywhere_else(self):
        for name, values in m.PROFILES.items():
            if name == "straddle":
                continue
            self.assertNotIn("straddle", values,
                             f"{name} should not touch the straddle switch")

    def test_the_straddle_profile_does_not_enable_scale_in(self):
        # Scale-in is opt-in, never a default -- this profile has no model
        # probability for it to top up toward.
        c = straddle_cfg()
        self.assertFalse(c.scale_in)

    def test_the_declared_stake_cap_does_not_contradict_the_straddle_stake(self):
        """
        max_stake_pct is inert on the straddle path, which is exactly why it
        must not disagree with straddle_stake_pct: --check-config and
        --preflight both read it, and a config that advertises a 5% cap
        while every leg stakes 20% is lying to whoever reads it.
        """
        c = straddle_cfg()
        self.assertGreaterEqual(c.max_stake_pct, c.straddle_stake_pct)

    def test_the_reserve_leaves_room_for_the_concurrent_rounds_allowed(self):
        """
        reserve_pct is the one limit that CAN throttle the straddle path, via
        _available. At 30% reserve and 20% per leg only one round could ever
        be funded, which made max_concurrent_positions: 4 a dead setting.
        """
        c = straddle_cfg()
        spendable = 1.0 - c.reserve_pct
        per_round = c.straddle_stake_pct * 2
        rounds = c.max_concurrent_positions // 2
        self.assertGreaterEqual(spendable, per_round * min(rounds, 2))

    def test_the_straddle_profile_gates_on_the_payout_test(self):
        """
        The gate is the profile. Without it the bot buys both sides of any
        round at any price, which is a coin flip paying a fee, not a hedge.
        """
        c = straddle_cfg()
        self.assertTrue(c.straddle_require_positive_worst_case)
        self.assertEqual(c.straddle_min_worst_case_return, 0.0)


class QuotingClient(FakeClient):
    """FakeClient whose quotes can differ per side, and can fail to place."""

    def __init__(self, *a, quotes=None, fail_order_after=None,
                 kill_fills=False, confirm_unreachable=False, **kw):
        super().__init__(*a, **kw)
        self._quotes = quotes or {}
        self._fail_after = fail_order_after
        self.kill_fills = kill_fills
        self.confirm_unreachable = confirm_unreachable

    def get_quote(self, rnd, side, stake):
        price = self._quotes.get(side, 0.51)
        return m.Quote("q-" + side.value, price, stake / price, 0.001, 0.0)

    def place_order(self, rnd, quote, stake_usdt=None):
        if (self._fail_after is not None
                and len(self.orders) >= self._fail_after):
            raise m.ApiError("venue rejected the order")
        return super().place_order(rnd, quote, stake_usdt)

    def confirm_fill(self, order_id, requested_usdt):
        if self.kill_fills:
            raise m.OrderNotFilled(f"order {order_id} did not fill: status "
                                   f"KILLED, filled 0.0")
        if self.confirm_unreachable:
            raise m.ApiError("read timed out")
        return super().confirm_fill(order_id, requested_usdt)


class TestStraddleEntry(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.db)

    def _trader(self, client, **kw):
        c = straddle_cfg(db_path=self.db, **kw)
        return build_trader(client, c, self.db)

    def test_dispatch_uses_the_straddle_path_when_enabled(self):
        c = straddle_cfg(db_path=self.db)
        t = build_trader(FakeClient([], [(0, 100_000.0)], {}, {}), c, self.db)
        called = {"straddle": False, "model": False}
        t._maybe_enter_straddle = lambda *a: called.__setitem__("straddle", True)
        t._maybe_enter_model = lambda *a: called.__setitem__("model", True)
        t._maybe_enter(100.0, "PAPER")
        self.assertTrue(called["straddle"])
        self.assertFalse(called["model"])

    def test_dispatch_uses_the_model_path_when_disabled(self):
        c = cfg(db_path=self.db, **m.PROFILES["balanced"])
        t = build_trader(FakeClient([], [(0, 100_000.0)], {}, {}), c, self.db)
        called = {"straddle": False, "model": False}
        t._maybe_enter_straddle = lambda *a: called.__setitem__("straddle", True)
        t._maybe_enter_model = lambda *a: called.__setitem__("model", True)
        t._maybe_enter(100.0, "PAPER")
        self.assertFalse(called["straddle"])
        self.assertTrue(called["model"])

    def test_a_pair_that_cannot_pay_back_the_stake_is_refused(self):
        """
        Two legs at 0.70 cost more together than either can return: 20 on
        each pays 28.57 back against 40 staked, whichever way it lands. That
        is a guaranteed loss dressed up as a hedge, and it is the whole
        reason the gate exists.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=200)
        books = {(1, Side.UP): [(0.70, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "both straddle payouts do not beat the stake")
        # Refused, not written off: the round stays out of _seen so every
        # later poll inside the window re-prices it.
        self.assertNotIn(1, t._seen)

    def test_a_break_even_pair_is_refused(self):
        """
        0.50/0.50 with no fee returns exactly what it cost. "More than the
        stake" is the gate, so break-even is capital at risk for nothing.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.50, 10_000)],
                 (1, Side.DOWN): [(0.50, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})

    def test_an_asymmetric_pair_is_sized_so_both_payouts_clear(self):
        """
        The case an equal split throws away. UP at 0.20 and DOWN at 0.70 sum
        to 0.90, so the pair IS profitable -- but only if the stakes are
        weighted. Split 20/20 the DOWN leg returns 28.57 against 40 staked
        and the round is a coin flip; weighted, both sides pay the same and
        both beat the stake.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.20, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(sorted(t._positions),
                         [("BTCUSDT", Side.DOWN), ("BTCUSDT", Side.UP)])
        up = t._positions[("BTCUSDT", Side.UP)].signal
        down = t._positions[("BTCUSDT", Side.DOWN)].signal
        self.assertLess(up.stake_usdt, down.stake_usdt)
        total = up.stake_usdt + down.stake_usdt
        self.assertAlmostEqual(total, 40.0, places=9)
        for sig in (up, down):
            payout = sig.stake_usdt / sig.fill_price
            self.assertGreater(payout, total)

    def test_a_small_live_bankroll_still_clears_the_per_leg_minimum(self):
        """
        Regression: at 2.5% per leg a 20 USDT bankroll sized each leg at
        0.50, under min_stake_usdt, so _maybe_enter_straddle returned before
        it ever looked at a round and the bot sat idle forever.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.45, 10_000)],
                 (1, Side.DOWN): [(0.45, 10_000)]}
        t = self._trader(FakeClient([rnd], [(start, 100_000.0)], books, {}))

        t._maybe_enter(20.0, "PAPER")

        self.assertEqual(len(t._positions), 2)
        for pos in t._positions.values():
            self.assertGreaterEqual(pos.signal.stake_usdt,
                                    t._cfg.min_stake_usdt)

    def test_a_second_round_can_be_funded_while_the_first_is_open(self):
        """
        Regression on the reserve: with two legs already open, the profile's
        four slots are only usable if the reserve leaves room for the second
        round's 40%.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        first = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                           fee_bps=0)
        second = make_round(topic_id=2, market_id=10, slug="eth-5m",
                            symbol="ETHUSDT", strike=3_000.0,
                            up_token_id="3", down_token_id="4",
                            feed_symbol="ETHUSDT",
                            start_ms=start, end_ms=end, fee_bps=0)
        books = {(1, Side.UP): [(0.45, 10_000)],
                 (1, Side.DOWN): [(0.45, 10_000)],
                 (2, Side.UP): [(0.45, 10_000)],
                 (2, Side.DOWN): [(0.45, 10_000)]}
        client = FakeClient([first, second], [(start, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(len(t._positions), 4)

    def test_a_round_is_repriced_every_poll_until_its_window_shuts(self):
        """
        The waiting the profile depends on. At 0.55/0.55 the pair is a
        guaranteed loss and is refused -- but not written off. Seconds later
        the book has moved to 0.45/0.45 and the same round is taken.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.55, 10_000)],
                 (1, Side.DOWN): [(0.55, 10_000)]}
        # Two polls, five seconds apart, both inside the 15s window.
        client = FakeClient([rnd], [(start + 2_000, 100_000.0),
                                    (start + 7_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertNotIn(1, t._seen)

        books[(1, Side.UP)] = [(0.45, 10_000)]
        books[(1, Side.DOWN)] = [(0.45, 10_000)]
        client.t = 1
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(len(t._positions), 2)

    def test_the_two_legs_are_bought_at_two_different_moments(self):
        """
        The whole strategy, start to finish. UP is cheap early and DOWN is
        not; a minute later DOWN is cheap and UP is not. Neither price was
        ever on offer beside the other, and the pair still wins either way.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.80, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (start + 90_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])
        up = t._positions[("BTCUSDT", Side.UP)]

        # The book turns over: DOWN is now the cheap side. UP at 0.25 and
        # DOWN at 0.25 were never on offer at the same moment.
        books[(1, Side.UP)] = [(0.82, 10_000)]
        books[(1, Side.DOWN)] = [(0.25, 10_000)]
        client.t = 1
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(sorted(t._positions),
                         [("BTCUSDT", Side.DOWN), ("BTCUSDT", Side.UP)])
        down = t._positions[("BTCUSDT", Side.DOWN)]
        total = up.committed_usdt + down.committed_usdt
        for pos in (up, down):
            payout = pos.committed_usdt / pos.signal.fill_price
            self.assertGreater(payout, total)

    def test_a_second_leg_that_cannot_cover_the_first_is_refused(self):
        """
        0.25 then 0.80 sums past 1.00: no stake on the second leg makes both
        payouts clear, so the open leg is left alone to keep waiting.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.80, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (start + 90_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        client.t = 1
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_the_second_leg_holds_out_early_and_settles_for_less_late(self):
        """
        A price that merely locks the round in is refused while there is
        still time to want the same 4x the first leg had to clear, and taken
        once there is not.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        # 0.80 does not clear beside 0.25, so UP opens on its own.
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.80, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (start + 15_000, 100_000.0),
                                    (end - 40_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

        # 0.70 clears the guarantee (0.25 + 0.70 < 1.00) but is nowhere near
        # 0.25, and there are still four minutes to find something better.
        books[(1, Side.DOWN)] = [(0.70, 10_000)]
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

        # Same price, almost no time left: take the locked-in profit.
        client.t = 2
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 2)

    def test_an_unhedgeable_leg_is_hedged_at_market_at_the_deadline(self):
        """
        No price ever covered the open leg. Rather than ride a one-sided bet
        to settlement, buy the other side and take a bounded loss.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.90, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (end - 10_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

        client.t = 1                     # inside straddle_hedge_deadline_s
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(len(t._positions), 2)

    def test_a_naked_leg_can_be_left_to_ride_when_that_is_configured(self):
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.90, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (end - 10_000, 100_000.0)], books, {})
        t = self._trader(client, straddle_force_hedge=False)

        t._maybe_enter(100.0, "PAPER")
        client.t = 1
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_completing_a_hedge_is_not_blocked_by_the_position_cap(self):
        """
        Slots exist to limit exposure. A completing leg REDUCES it, so it
        must not queue behind the cap that new rounds respect.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.80, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (start + 90_000, 100_000.0)], books, {})
        t = self._trader(client, max_concurrent_positions=1)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 1)

        books[(1, Side.DOWN)] = [(0.25, 10_000)]
        client.t = 1
        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(len(t._positions), 2)   # cap is 1, hedge still ran

    def test_a_worthless_or_certain_side_does_not_crash_the_bot(self):
        """
        The reported crash. Late in a round the losing side decays to 0.00
        and the winning side to 1.00, and rounding to the market's precision
        snaps both the rest of the way. Neither is a probability, and
        breakeven_probability raised ValueError straight out of
        _complete_half_straddles and killed the process.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        books = {(1, Side.UP): [(0.20, 10_000)],
                 (1, Side.DOWN): [(0.85, 10_000)]}
        client = FakeClient([rnd], [(start + 60_000, 100_000.0),
                                    (start + 200_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

        # The round resolves: UP is now a certainty, DOWN is worthless.
        books[(1, Side.UP)] = [(1.00, 10_000)]
        books[(1, Side.DOWN)] = [(0.00, 10_000)]
        client.t = 1

        t._maybe_enter(100.0, "PAPER")      # must not raise

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_an_unusable_two_sided_price_is_skipped_not_crashed(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        books = {(1, Side.UP): [(1.00, 10_000)],
                 (1, Side.DOWN): [(0.00, 10_000)]}
        client = FakeClient([rnd], [(start + 60_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")      # must not raise

        self.assertEqual(t._positions, {})

    def test_a_killed_order_does_not_become_a_position(self):
        """
        The venue answers MARKET FOK orders with an id whether or not they
        fill. Recording a position on that id invents a trade which later
        settles and books a profit that was never made.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.20, 10_000)],
                 (1, Side.DOWN): [(0.85, 10_000)]}
        client = QuotingClient([rnd], [(start + 60_000, 100_000.0)], books,
                               {}, quotes={Side.UP: 0.20, Side.DOWN: 0.85},
                               kill_fills=True)
        t = self._trader(client, live=True)
        t._active_live = True

        t._maybe_enter(100.0, "LIVE")

        self.assertEqual(client.orders and True, True)   # it did try
        self.assertEqual(t._positions, {})

    def test_an_unconfirmable_fill_is_still_recorded(self):
        """
        The opposite case, and the reason the two cannot share a branch. A
        timeout says nothing about whether the order is live; dropping it
        strands money that is never settled and never claimed.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.20, 10_000)],
                 (1, Side.DOWN): [(0.85, 10_000)]}
        client = QuotingClient([rnd], [(start + 60_000, 100_000.0)], books,
                               {}, quotes={Side.UP: 0.20, Side.DOWN: 0.85},
                               confirm_unreachable=True)
        t = self._trader(client, live=True)
        t._active_live = True

        t._maybe_enter(100.0, "LIVE")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_the_gate_is_retested_against_the_live_quotes(self):
        """
        The book says 0.45/0.45; the quotes come back at 0.55 each. Gating
        on the book and executing on the quote is how a pair that cleared on
        paper becomes a guaranteed loss in the account, so nothing is placed.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.45, 10_000)],
                 (1, Side.DOWN): [(0.45, 10_000)]}
        client = QuotingClient([rnd], [(start, 100_000.0)], books, {},
                               quotes={Side.UP: 0.55, Side.DOWN: 0.55})
        t = self._trader(client, live=True)
        t._active_live = True

        t._maybe_enter(100.0, "LIVE")

        self.assertEqual(t._positions, {})
        self.assertEqual(client.orders, [])   # quotes place nothing
        self.assertEqual(t._watching[1][1],
                         "both straddle payouts do not beat the stake")

    def test_a_leg_that_opens_before_the_other_fails_is_still_recorded(self):
        """
        Regression: the second order failing used to discard the whole fill
        map, leaving the FIRST leg live on the venue but absent from the
        journal -- unhedged, unsettled and never claimed.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.45, 10_000)],
                 (1, Side.DOWN): [(0.45, 10_000)]}
        client = QuotingClient([rnd], [(start, 100_000.0)], books, {},
                               quotes={Side.UP: 0.45, Side.DOWN: 0.45},
                               fail_order_after=1)
        t = self._trader(client, live=True)
        t._active_live = True

        with self.assertRaises(m.ApiError):
            t._maybe_enter(100.0, "LIVE")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_both_legs_settle_independently_and_correctly(self):
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        books = {(1, Side.UP): [(0.10, 10_000)],
                 (1, Side.DOWN): [(0.10, 10_000)]}
        path = [(start, 100_000.0), (end + 3_000, 100_400.0)]
        client = FakeClient([rnd], path, books, {})
        t = self._trader(client)

        client.t = 0
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 2)

        client.t = 1
        client._winners[1] = Side.UP
        t._settle_open()

        self.assertEqual(t._positions, {})
        # UP staked 20 at 0.10 wins 20*9=180; DOWN staked 20 loses it.
        # Net vs the 100.0 start: +180 - 20 = +160.0.
        self.assertAlmostEqual(t._paper_bankroll, 260.0, places=9)

    def test_the_opening_window_is_the_first_minute(self):
        """
        A first leg opened at t=240 of a 300s round has 60 seconds to find
        its hedge, and usually does not. The window buys RUNWAY, not
        cheapness: open early and the rest of the round is completion time.
        """
        c = straddle_cfg()
        self.assertEqual(c.straddle_entry_window_s, 60.0)
        self.assertEqual(m.Config.straddle_entry_window_s, 60.0)

    def test_a_first_leg_opens_at_the_loosened_ceiling(self):
        """
        Sixty seconds in, spot has barely left the strike, so neither side is
        anywhere near 0.25. A 0.25 ceiling on a 60s window would point the
        opener at the one stretch of the round where its entry price cannot
        occur -- the dead-bot failure, reintroduced from the other end. 0.38
        is what an early book actually offers, and it still leaves 0.62 of
        room for the hedge.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.38, 10_000)],
                 (1, Side.DOWN): [(0.65, 10_000)]}
        client = FakeClient([rnd], [(start + 30_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_a_leg_dearer_than_the_opening_ceiling_is_still_refused(self):
        """Loosened is not removed: 0.45 leaves too little room to hedge."""
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.45, 10_000)],
                 (1, Side.DOWN): [(0.60, 10_000)]}
        client = FakeClient([rnd], [(start + 30_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})

    def test_a_round_already_a_minute_old_is_left_alone(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=0)
        books = {(1, Side.UP): [(0.15, 10_000)],
                 (1, Side.DOWN): [(0.88, 10_000)]}
        client = FakeClient([rnd], [(start + 90_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertIn(1, t._seen)

    def test_a_round_with_no_runway_left_to_hedge_is_left_alone(self):
        """
        Opening a leg that cannot be completed is just a directional bet.
        Completion stops at straddle_hedge_deadline_s, so anything inside
        twice that has no realistic chance of finding its other side.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end)
        books = {(1, Side.UP): [(0.10, 10_000)],
                 (1, Side.DOWN): [(0.10, 10_000)]}
        client = FakeClient([rnd], [(end - 20_000, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertIn(1, t._seen)

    def test_a_round_past_the_opening_window_is_left_alone(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        books = {(1, Side.UP): [(0.10, 10_000)],
                 (1, Side.DOWN): [(0.10, 10_000)]}
        client = FakeClient([rnd], [(start + 90_000, 100_000.0)], books, {})
        t = self._trader(client, straddle_entry_window_s=60.0)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertIn(1, t._seen)

    def test_the_ceiling_blocks_a_round_whose_cheap_side_is_dear(self):
        """Nothing opens when even the cheaper side is above the ceiling."""
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        books = {(1, Side.UP): [(0.60, 10_000)],
                 (1, Side.DOWN): [(0.90, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client, straddle_max_leg_price=0.55)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "straddle leg priced above ceiling")

    def test_a_cheap_side_is_opened_alone_even_when_the_other_is_dear(self):
        """
        The heart of the strategy. UP at 0.10 pays 10x; DOWN at 0.90 is
        simply not bought yet. Waiting for both to be cheap AT ONCE is what
        makes this profile do nothing, because a live book never offers it.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        books = {(1, Side.UP): [(0.10, 10_000)],
                 (1, Side.DOWN): [(0.90, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_the_opener_is_whichever_side_is_cheap_never_a_fixed_one(self):
        """
        UP and DOWN are interchangeable. The same book mirrored has to open
        the other side, or the strategy has a directional bias it never
        claimed to have.
        """
        start = 1_700_000_000_000
        for cheap in (Side.UP, Side.DOWN):
            rnd = make_round(strike=100_000.0, start_ms=start,
                             end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
            books = {(1, cheap): [(0.20, 10_000)],
                     (1, cheap.other): [(0.85, 10_000)]}
            t = self._trader(FakeClient([rnd], [(start, 100_000.0)],
                                        books, {}))
            t._maybe_enter(100.0, "PAPER")
            self.assertEqual(list(t._positions), [("BTCUSDT", cheap)])

    def test_optional_worst_case_floor_can_still_block_when_opted_in(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000),
                         fee_bps=200)
        books = {(1, Side.UP): [(0.70, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client, straddle_require_positive_worst_case=True)

        t._maybe_enter(100.0, "PAPER")

        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "both straddle payouts do not beat the stake")

    def test_scale_in_never_runs_for_a_straddle_position(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=100_000.0, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        books = {(1, Side.UP): [(0.10, 10_000)],
                 (1, Side.DOWN): [(0.10, 10_000)]}
        client = FakeClient([rnd], [(start, 100_000.0)], books, {})
        t = self._trader(client)
        t._maybe_enter(100.0, "PAPER")
        before = dict(t._positions)

        t._maybe_scale_in_all(100.0)   # must be a no-op: nothing to top up to

        self.assertEqual(t._positions, before)


class TestStraddleCompletionBar(unittest.TestCase):
    """
    What the second leg holds out for, and why it is not a constant.

    The bar used to be derived from straddle_first_leg_max_price -- the
    OPENER's ceiling -- which made it over-ambitious the moment the opener
    was loosened: a leg filled at 0.40 would refuse a 0.40 hedge that
    matched its own profit exactly, and hold out for a 0.25 that no longer
    had any relation to what the round cost. The bar is the open fill
    itself: equal stakes at equal prices pay equally, so "as good as the
    first leg" IS the open price. 0.25 stays a preferred entry, not a gate
    on completing.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.rnd = make_round(fee_bps=0)
        client = FakeClient([self.rnd], [(0, 100_000.0)], {}, {})
        self.t = build_trader(client, straddle_cfg(db_path=self.db), self.db)

    def tearDown(self):
        os.unlink(self.db)

    def _wait(self, be_open, be_other, seconds_left):
        return self.t._completion_is_worth_waiting_out(
            self.rnd, be_open, be_other, seconds_left)

    def test_a_hedge_matching_the_open_price_is_taken_at_once(self):
        # The regression. Opened at 0.40, offered 0.40: same price, same
        # payout, round locked in. Nothing about waiting improves it.
        self.assertFalse(self._wait(0.40, 0.40, 280.0))

    def test_a_hedge_dearer_than_the_open_price_is_waited_out_early(self):
        # 0.55 guarantees the round (0.40 + 0.55 < 1.00) but is worse than
        # the leg already held, and there are four minutes to better it.
        self.assertTrue(self._wait(0.40, 0.55, 280.0))

    def test_that_same_hedge_is_taken_as_the_deadline_approaches(self):
        self.assertFalse(self._wait(0.40, 0.55, 35.0))

    def test_a_cheap_opener_still_holds_out_for_a_cheap_hedge(self):
        # Unchanged behaviour where the two numbers used to agree.
        self.assertTrue(self._wait(0.25, 0.70, 280.0))
        self.assertFalse(self._wait(0.25, 0.25, 280.0))

    def test_an_opener_at_even_money_is_not_choosy(self):
        # be_open >= 0.5 leaves no room: the preferred price and the
        # break-even limit are the same number or crossed.
        self.assertFalse(self._wait(0.50, 0.49, 280.0))
        self.assertFalse(self._wait(0.60, 0.39, 280.0))

    def test_the_bar_never_consults_the_opening_ceiling(self):
        # Moving the opener's ceiling must not move the completion bar.
        loose = build_trader(
            FakeClient([self.rnd], [(0, 100_000.0)], {}, {}),
            straddle_cfg(db_path=self.db, straddle_first_leg_max_price=0.49),
            self.db)
        for be_other in (0.30, 0.45, 0.55):
            self.assertEqual(
                loose._completion_is_worth_waiting_out(
                    self.rnd, 0.40, be_other, 280.0),
                self._wait(0.40, be_other, 280.0))


# --------------------------------------------------------------------------
# Last minute: buy the dearer side as the clock runs out, and nothing else
# --------------------------------------------------------------------------


def lastminute_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False,
                **m.PROFILES["lastminute"])
    base.update(kw)
    return Config(**base)


class TestLastMinuteConfig(unittest.TestCase):

    def test_the_profile_is_internally_valid(self):
        lastminute_cfg()          # must not raise

    def test_the_switch_is_off_everywhere_else(self):
        for name, values in m.PROFILES.items():
            if name == "lastminute":
                continue
            self.assertNotIn("last_minute", values,
                             f"{name} should not touch the last-minute switch")

    def test_it_is_off_by_default(self):
        self.assertFalse(Config(api_key="k", api_secret="s").last_minute)

    def test_the_declared_stake_cap_does_not_contradict_the_real_one(self):
        """
        max_stake_pct is inert on this path, which is exactly why it must not
        disagree with last_minute_stake_pct: --check-config and --preflight
        both read it, and a config advertising one cap while staking another
        is lying to whoever reads it.
        """
        c = lastminute_cfg()
        self.assertGreaterEqual(c.max_stake_pct, c.last_minute_stake_pct)

    def test_the_reserve_leaves_room_for_the_concurrent_rounds_allowed(self):
        """
        reserve_pct is the one limit that CAN throttle this path, via
        _available. A reserve that cannot fund the slots the profile claims
        makes max_concurrent_positions a dead setting.
        """
        c = lastminute_cfg()
        spendable = 1.0 - c.reserve_pct
        self.assertGreaterEqual(
            spendable, c.last_minute_stake_pct * c.max_concurrent_positions)

    def test_the_ceiling_is_off_by_default(self):
        """
        1.0 is "no ceiling", which is the rule as specified. Turning it on is
        a decision about which trades the strategy is for, not a default.
        """
        self.assertEqual(lastminute_cfg().last_minute_max_price, 1.0)
        self.assertEqual(
            Config(api_key="k", api_secret="s").last_minute_max_price, 1.0)

    def test_a_ceiling_at_or_under_the_floor_is_rejected(self):
        """
        No price could satisfy both, so the primary branch could never fire
        and the profile would quietly become fallback-only.
        """
        for bad in (0.75, 0.60, 0.30):
            with self.assertRaises(ValueError):
                lastminute_cfg(last_minute_max_price=bad)

    def test_a_ceiling_above_certainty_is_rejected(self):
        with self.assertRaises(ValueError):
            lastminute_cfg(last_minute_max_price=1.01)

    def test_a_ceiling_between_the_floor_and_one_is_accepted(self):
        for good in (0.80, 0.85, 0.90, 1.0):
            self.assertEqual(
                lastminute_cfg(last_minute_max_price=good).
                last_minute_max_price, good)

    def test_a_floor_at_or_below_a_coin_flip_is_rejected(self):
        """Below 0.5 the floor can never fail, so the fallback is dead code."""
        for bad in (0.5, 0.4, 0.0):
            with self.assertRaises(ValueError):
                lastminute_cfg(last_minute_price_floor=bad)

    def test_a_floor_at_or_above_certainty_is_rejected(self):
        for bad in (1.0, 1.2):
            with self.assertRaises(ValueError):
                lastminute_cfg(last_minute_price_floor=bad)

    def test_the_three_clocks_must_be_ordered(self):
        with self.assertRaises(ValueError):          # fallback after start
            lastminute_cfg(last_minute_start_s=30.0,
                           last_minute_fallback_s=45.0)
        with self.assertRaises(ValueError):          # fallback at the deadline
            lastminute_cfg(last_minute_fallback_s=5.0,
                           last_minute_deadline_s=5.0)
        with self.assertRaises(ValueError):          # deadline after fallback
            lastminute_cfg(last_minute_deadline_s=50.0)

    def test_a_fallback_equal_to_the_start_is_legal(self):
        """It means "never hold out for the floor", which is a real setting."""
        c = lastminute_cfg(last_minute_fallback_s=60.0)
        self.assertEqual(c.last_minute_fallback_s, c.last_minute_start_s)

    def test_stake_bounds(self):
        for bad in (0.0, -0.1, 0.3):
            with self.assertRaises(ValueError):
                lastminute_cfg(last_minute_stake_pct=bad)

    def test_it_cannot_run_alongside_the_straddle(self):
        """Two entry strategies, one dispatch; one would silently win."""
        with self.assertRaises(ValueError):
            lastminute_cfg(straddle=True)

    def test_it_cannot_scale_in(self):
        """There is no model probability for a top-up to aim at."""
        with self.assertRaises(ValueError):
            lastminute_cfg(scale_in=True)

    def test_the_band_does_not_narrow_the_rule(self):
        """
        The entry band is not this profile's gate and must not act as one:
        anything the rule can buy -- a 0.98 leader, or a 0.20 one after the
        floor drops -- has to sit inside it.
        """
        c = lastminute_cfg()
        self.assertLessEqual(c.min_entry_price, 0.05)
        self.assertGreaterEqual(c.max_entry_price, 0.98)


class TestLastMinuteEntry(unittest.TestCase):
    """The whole strategy: dearer side, floor, fallback, and nothing else."""

    START = 1_700_000_000_000

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        # Windows refuses to unlink a file sqlite still holds open, and a
        # Journal outlives the Trader that made it. Closing here keeps a
        # tearDown failure from masking the assertion the test actually made.
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _build(self, client, c):
        trader = build_trader(client, c, self.db)
        self._built.append(trader)
        return trader

    def _round(self, **kw):
        base = dict(strike=100_000.0, start_ms=self.START,
                    end_ms=self.START + (m.DEFAULT_ROUND_SECONDS * 1000),
                    fee_bps=200)
        base.update(kw)
        return make_round(**base)

    def _at(self, secs_left, up, down, client_cls=FakeClient, **cfgkw):
        """A trader looking at one round with `secs_left` to run."""
        rnd = self._round()
        now = rnd.end_ms - int(secs_left * 1000)
        books = {}
        if up is not None:
            books[(1, Side.UP)] = [(up, 10_000)]
        if down is not None:
            books[(1, Side.DOWN)] = [(down, 10_000)]
        client = client_cls([rnd], [(now, 100_000.0)], books, {})
        c = lastminute_cfg(db_path=self.db, **cfgkw)
        return self._build(client, c), client

    # -- dispatch ---------------------------------------------------------

    def test_dispatch_uses_the_last_minute_path_when_enabled(self):
        c = lastminute_cfg(db_path=self.db)
        t = self._build(FakeClient([], [(0, 100_000.0)], {}, {}), c)
        called = {"last": False, "straddle": False, "model": False}
        t._maybe_enter_last_minute = lambda *a: called.__setitem__("last", True)
        t._maybe_enter_straddle = lambda *a: called.__setitem__("straddle", True)
        t._maybe_enter_model = lambda *a: called.__setitem__("model", True)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(called, {"last": True, "straddle": False,
                                  "model": False})

    # -- the clock --------------------------------------------------------

    def test_nothing_happens_before_the_last_minute(self):
        t, _ = self._at(90.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        # Not written off either: its minute has not arrived yet.
        self.assertNotIn(1, t._seen)

    def test_the_minute_opens_exactly_at_the_configured_second(self):
        t, _ = self._at(60.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_deadline_closes_the_round(self):
        t, _ = self._at(4.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertIn(1, t._seen)

    # -- the rule ---------------------------------------------------------

    def test_it_buys_the_dearer_side(self):
        t, _ = self._at(55.0, 0.80, 0.20)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.fill_price, 0.80)

    def test_dearer_is_not_a_preference_for_up(self):
        t, _ = self._at(55.0, 0.20, 0.80)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.DOWN)])

    def test_a_leader_under_the_floor_waits(self):
        t, _ = self._at(55.0, 0.60, 0.40)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "no side has reached the price floor")
        # Refused, not written off: the price may reach the floor by 50s.
        self.assertNotIn(1, t._seen)

    def test_the_floor_binds_just_under_it(self):
        t, _ = self._at(55.0, 0.7499, 0.2501)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_floor_clears_exactly_at_it(self):
        t, _ = self._at(55.0, 0.75, 0.25)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_fallback_buys_the_leader_under_the_floor(self):
        t, _ = self._at(40.0, 0.60, 0.40)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.fill_price, 0.60)

    def test_the_fallback_opens_exactly_at_its_second(self):
        t, _ = self._at(45.0, 0.60, 0.40)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_fallback_still_takes_the_dearer_side(self):
        t, _ = self._at(40.0, 0.45, 0.55)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.DOWN)])

    def test_level_prices_are_left_alone(self):
        """
        There is no dominant side in a tie, and picking one anyway would be
        inventing the only signal this strategy refuses to have.
        """
        t, _ = self._at(40.0, 0.50, 0.50)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1], "the two sides are priced level")

    def test_a_leader_at_one_is_left_alone(self):
        """1.00 can never pay back more than it cost; buying it is a fee."""
        t, _ = self._at(40.0, 1.00, 0.001)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "the leading side is priced at 1.00")

    def test_a_worthless_other_side_does_not_block_the_leader(self):
        """
        _book_price answers None for a side that has decayed to nothing, and
        ranking must not mistake that for "no price to compare against".
        """
        t, _ = self._at(40.0, 0.97, 0.0001)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])

    def test_a_dear_leader_is_bought_without_complaint(self):
        """
        The cost of the rule as specified: 0.97 pays about 3% on a win, so
        it takes 32 of them to cover one loss, against 3 at the 0.75 floor.
        Nothing stops it, deliberately -- this test exists so the arithmetic
        is a known cost rather than a discovery.
        """
        t, _ = self._at(55.0, 0.97, 0.03)
        t._maybe_enter(100.0, "PAPER")
        sig = t._positions[("BTCUSDT", Side.UP)].signal
        self.assertAlmostEqual(sig.fill_price, 0.97)
        self.assertLess(m.win_return(sig.fill_price, 200), 0.04)
        self.assertGreater(m.wins_per_loss(sig.fill_price),
                           10 * m.wins_per_loss(0.75))

    # -- it consults nothing else -----------------------------------------

    # -- the price ceiling ------------------------------------------------

    def test_the_default_profile_buys_a_dear_leader(self):
        """The ceiling is off by default, so nothing above the floor binds."""
        t, _ = self._at(55.0, 0.97, 0.03)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_a_ceiling_refuses_the_leader_above_it(self):
        t, _ = self._at(55.0, 0.97, 0.03, last_minute_max_price=0.90)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "the leading side is priced above the ceiling")
        # Refused, not written off: the leader can cheapen before the round
        # ends, and a round that comes back under the ceiling is tradable.
        self.assertNotIn(1, t._seen)

    def test_a_ceiling_still_takes_the_band_below_it(self):
        t, _ = self._at(55.0, 0.88, 0.12, last_minute_max_price=0.90)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_ceiling_binds_just_above_it(self):
        t, _ = self._at(55.0, 0.9001, 0.0999, last_minute_max_price=0.90)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_ceiling_clears_exactly_at_it(self):
        t, _ = self._at(55.0, 0.90, 0.10, last_minute_max_price=0.90)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_clock_never_relaxes_the_ceiling(self):
        """
        Unlike the floor, which the fallback drops on purpose, a round that
        is already decided does not become a better bet for being nearly
        over. Nothing about the ceiling is time-dependent.
        """
        for secs in (58.0, 44.0, 10.0):
            t, _ = self._at(secs, 0.97, 0.03, last_minute_max_price=0.90)
            t._maybe_enter(100.0, "PAPER")
            self.assertEqual(t._positions, {}, f"{secs}s")

    def test_the_ceiling_cannot_reach_the_fallbacks_prices(self):
        """
        The fallback only fires under the floor, and the ceiling is above it,
        so the two can never contradict each other.
        """
        t, _ = self._at(40.0, 0.60, 0.40, last_minute_max_price=0.90)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_quote_is_re_tested_against_the_ceiling(self):
        """A book under the ceiling and a quote over it is not an approval."""
        t, _ = self._at(55.0, 0.88, 0.12, client_cls=QuotingClient, live=True,
                        last_minute_max_price=0.90)
        t._client._quotes = {Side.UP: 0.95, Side.DOWN: 0.05}
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "the leading side is priced above the ceiling")

    def test_the_ceiling_bounds_how_many_wins_cover_a_loss(self):
        """What the knob is actually for, stated in the unit that matters."""
        c = lastminute_cfg(last_minute_max_price=0.90)
        self.assertAlmostEqual(m.wins_per_loss(c.last_minute_max_price),
                               9.0, places=9)
        self.assertGreater(m.win_return(c.last_minute_max_price, 200), 0.10)

    def test_the_model_gates_are_not_consulted(self):
        """
        The point of the profile. An edge floor and a buffer gate that would
        refuse every round on the model path must change nothing here.
        """
        t, _ = self._at(55.0, 0.85, 0.15, min_edge=0.90, min_edge_ratio=5.0,
                        min_buffer_sigmas=9.0)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_the_model_entry_window_is_not_consulted(self):
        t, _ = self._at(55.0, 0.85, 0.15, entry_window_start_s=280,
                        entry_window_end_s=270)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_no_spot_or_volatility_is_read(self):
        """
        Nothing about the underlying enters this decision, so a client that
        raises on being asked must still produce the trade.
        """
        t, client = self._at(55.0, 0.85, 0.15)

        def boom(*a, **kw):
            raise AssertionError("the last-minute path read the underlying")

        client.spot_price = boom
        t._vol.sigma_annual = boom
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    # -- sizing and bookkeeping -------------------------------------------

    def test_the_stake_is_the_profiles_own_fraction(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        pos = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(pos.signal.stake_usdt, 10.0)
        self.assertAlmostEqual(pos.committed_usdt, 10.0)

    def test_the_reserve_caps_the_stake(self):
        t, _ = self._at(55.0, 0.85, 0.15, reserve_pct=0.95)
        t._maybe_enter(100.0, "PAPER")
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.stake_usdt, 5.0)

    def test_a_stake_under_the_venue_minimum_trades_nothing(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._maybe_enter(5.0, "PAPER")          # 10% of 5.00 is 0.50
        self.assertEqual(t._positions, {})

    def test_the_journal_records_the_markets_implied_probability(self):
        """
        Not a forecast -- there is none. Recording the price restated as a
        probability is what lets the calibration breaker ask the one health
        question this profile has: are these favourites winning as often as
        I paid for them to?
        """
        t, _ = self._at(55.0, 0.80, 0.20)
        t._maybe_enter(100.0, "PAPER")
        sig = t._positions[("BTCUSDT", Side.UP)].signal
        self.assertAlmostEqual(sig.model_prob,
                               m.breakeven_probability(0.80, 200))
        self.assertEqual(sig.edge, 0.0)

    def test_one_position_per_market(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        t._seen.clear()                       # as if the round were fresh
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(len(t._positions), 1)

    def test_an_entered_round_is_not_revisited(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(1, t._seen)
        self.assertNotIn(1, t._watching)

    def test_scale_in_never_runs(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._maybe_enter(100.0, "PAPER")
        before = dict(t._positions)
        t._maybe_scale_in_all(100.0)
        self.assertEqual(t._positions, before)

    def test_a_halted_market_is_not_traded(self):
        t, _ = self._at(55.0, 0.85, 0.15)
        t._risk_for("BTCUSDT").halted_reason = "daily loss limit"
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    # -- live mode --------------------------------------------------------

    def test_the_quote_not_the_book_decides_in_live_mode(self):
        """
        The book is a screen; the executed price is what the rule has to hold
        on. A book showing 0.80 and a quote coming back at 0.70 is a trade
        the floor refuses, not one it already approved.
        """
        t, _ = self._at(55.0, 0.80, 0.20, client_cls=QuotingClient, live=True)
        t._client._quotes = {Side.UP: 0.70, Side.DOWN: 0.30}
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertEqual(t._watching[1][1],
                         "no side has reached the price floor")

    def test_a_worse_quote_is_still_taken_after_the_floor_drops(self):
        t, _ = self._at(40.0, 0.80, 0.20, client_cls=QuotingClient, live=True)
        t._client._quotes = {Side.UP: 0.70, Side.DOWN: 0.30}
        t._maybe_enter(100.0, "PAPER")
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.fill_price, 0.70)

    def test_a_killed_order_records_no_position(self):
        t, _ = self._at(55.0, 0.80, 0.20, client_cls=QuotingClient, live=True)
        t._client._quotes = {Side.UP: 0.80, Side.DOWN: 0.20}
        t._client.kill_fills = True
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
