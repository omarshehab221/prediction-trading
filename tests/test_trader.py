"""
The loop and its machinery: sessions, positions, resting orders,
exits, settlement, reconciliation and scaling in.
"""

from __future__ import annotations

import os
import random
import re
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config,
    Journal,
    Position,
    PredictionClient,
    Side,
    Signal,
    assess,
    kelly_stake,
    settle_pnl,
)
from tests.support import (
    FakeClient,
    ScalpClient,
    _FakePosition,
    _close_journals,
    build_trader,
    cfg,
    make_pending,
    make_position,
    make_round,
    make_signal,
    make_trader,
    scalp_cfg,
)
from tests.test_venue import TestParseRound

class TestSimulatedSession(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestLiveQuoteGate(unittest.TestCase):
    """A venue quote worse than the screen must abort the trade."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
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
        client.get_quote = lambda r, p: m.Quote("q", 0.93, 1.0, 0.0, 0.0)
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
        client.get_quote = lambda r, p: m.Quote("q", 0.88, 1.0, 0.0, 0.0)

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
        client.get_quote = lambda r, p: m.Quote("q", 0.55, 1.0, 0.40, 0.0)

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


class TestTieSettlement(unittest.TestCase):
    """An exact close at the strike must not be guessed on a real position."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestScaleIn(unittest.TestCase):
    """
    Top up while winning -- the opposite of martingale, which adds after
    losses. The target is recomputed from the current probability, so total
    exposure to one round stays bounded by Kelly.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestScaleInSizing(unittest.TestCase):
    """The top-up should be the larger bet -- bounded, not unbounded."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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
        _close_journals(self)
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
        _close_journals(self)
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
                # A loss: the stake left at entry, so nothing should move.
                # A win cannot be used to make this point any more -- its
                # payout has not landed when _reconcile runs, so there is
                # nothing yet to disagree with.
                t._reconcile(pos, False, -1.0, 100.0, 99.80)
            finally:
                m.LOG.removeHandler(handler); m.LOG.setLevel(prev)
            self.assertTrue(any("RECONCILE MISMATCH" in r for r in records))
        finally:
            _close_journals()
            os.unlink(db)

    def test_a_settlement_mismatch_is_reported_and_not_booked(self):
        """
        A balance read either side of one settlement is a useful SIGNAL and
        an unfit LEDGER ENTRY: the window is too narrow for a deposit and
        still too wide for attribution, because anything else landing inside
        it lands on this round's account. Squaring the books is left to
        reconcile(), which runs only with a flat book.
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

            t._reconcile(pos, False, -1.0, 100.0, 99.85)

            self.assertAlmostEqual(
                t._risk_for(rnd.symbol).pnl_correction, 0.0, places=9)
            self.assertAlmostEqual(
                t._account_risk.pnl_correction, 0.0, places=9)
        finally:
            _close_journals()
            os.unlink(db)

    def test_an_uncredited_win_is_never_charged_its_own_payout(self):
        """
        The bug that made this worse than the drift it was fixing. A win's
        payout lands minutes after settlement, so the balance has not moved
        by it yet; the correction on offer was therefore MINUS THE WHOLE
        PAYOUT, guarded only by the balance being unchanged to the last EPS.

        A straddle settles its second leg moments after its first, so the
        window routinely contains someone else's movement. Here that is a
        0.02 dusting -- enough to defeat an exact-zero guard, nowhere near
        enough to mean the 1.67 payout evaporated.
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

            t._reconcile(pos, True, 0.65, 100.0, 100.02)

            self.assertAlmostEqual(
                t._risk_for(rnd.symbol).pnl_correction, 0.0, places=9)
            self.assertAlmostEqual(
                t._account_risk.pnl_correction, 0.0, places=9)
        finally:
            _close_journals()
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
            _close_journals()
            os.unlink(db)

    def test_an_uncredited_win_corrects_nothing_yet(self):
        """
        The payout lands after this runs, so there is nothing to compare
        against. Correcting here would book the whole gross payout as a
        modelling error. Same claim as the test above, with an undisturbed
        window -- the property must not depend on that.
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
            _close_journals()
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
            _close_journals()
            os.unlink(db)


class TestPendingOrderLifecycle(unittest.TestCase):
    """An order that is neither filled nor dead is a state, not an error."""

    def tearDown(self):
        _close_journals(self)

    def _trader(self, states, **overrides):
        t = make_trader(**overrides)
        t._client.order_state = lambda oid: states.get(oid)
        t.cancelled = []

        def cancel(ids):
            t.cancelled.extend(ids)
            return list(ids), {}

        t._client.cancel_orders = cancel
        # The loop reads the clock; pin it two minutes before the round ends
        # so "expired" is decided by the pending order, not by drift.
        rnd = make_round()
        t._client.now_ms = lambda: rnd.end_ms - 120_000
        return t

    def test_a_resting_order_survives_a_reap(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertIn("o1", t._pending)
        self.assertEqual(t.cancelled, [])
        self.assertEqual(t._positions, {})

    def test_an_order_the_venue_has_never_heard_of_is_not_dropped(self):
        """None means the history is lagging, not that the order died."""
        t = self._trader({})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertIn("o1", t._pending)

    def test_a_filled_order_becomes_a_position_and_leaves_pending(self):
        t = self._trader({"o1": m.OrderState("FILLED", 5.0, 12.5, 0.40)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._reap_pending()
        self.assertNotIn("o1", t._pending)
        self.assertEqual(len(t._positions), 1)

    def test_the_window_closing_cancels_the_order(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=-1)
        t._reap_pending()
        self.assertEqual(t.cancelled, ["o1"])
        self.assertNotIn("o1", t._pending)

    def test_round_end_cancels_even_inside_the_window(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        rnd = make_round()
        t._client.now_ms = lambda: rnd.end_ms + 1
        t._pending["o1"] = make_pending("o1", expires_in_ms=999_999)
        t._reap_pending()
        self.assertEqual(t.cancelled, ["o1"])

    def test_drain_cancels_everything_resting(self):
        t = self._trader({"o1": m.OrderState("RESTING", 0.0, 0.0, None)})
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000)
        t._drain(timeout_s=0.0)
        self.assertEqual(t.cancelled, ["o1"])


class TestPartialFills(unittest.TestCase):
    """A partial fill cannot be refused: the shares are already ours."""

    def tearDown(self):
        _close_journals(self)

    def _trader(self, state_or_iter, **overrides):
        t = make_trader(**overrides)
        if callable(state_or_iter):
            t._client.order_state = state_or_iter
        else:
            t._client.order_state = lambda oid: state_or_iter
        t._client.cancel_orders = lambda ids: (list(ids), {})
        rnd = make_round()
        t._client.now_ms = lambda: rnd.end_ms - 120_000
        return t

    def test_a_partial_fill_is_recorded_at_its_real_size(self):
        t = self._trader(m.OrderState("PARTIAL", 2.0, 5.0, 0.40))
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        pos = next(iter(t._positions.values()))
        self.assertAlmostEqual(pos.committed_usdt, 2.0)
        self.assertAlmostEqual(pos.signal.fill_price, 0.40)

    def test_min_fill_fraction_is_not_consulted_for_a_limit_order(self):
        """
        It is the FOK guard, where a short fill means something went wrong.
        On a GTC order a short fill is the ordinary outcome, and refusing it
        strands shares the account already holds.
        """
        t = self._trader(m.OrderState("PARTIAL", 0.5, 1.25, 0.40),
                         min_fill_fraction=0.90)
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        self.assertEqual(len(t._positions), 1)

    def test_a_further_fill_extends_rather_than_duplicates(self):
        fills = iter([m.OrderState("PARTIAL", 2.0, 5.0, 0.40),
                      m.OrderState("FILLED", 5.0, 12.5, 0.40)])
        t = self._trader(lambda oid: next(fills))
        t._pending["o1"] = make_pending("o1", expires_in_ms=60_000, amount=5.0)
        t._reap_pending()
        t._reap_pending()
        self.assertEqual(len(t._positions), 1)
        pos = next(iter(t._positions.values()))
        self.assertAlmostEqual(pos.committed_usdt, 5.0)


class TestCancelRacesFill(unittest.TestCase):
    """The usual reason a cancel fails is that the order filled first."""

    def tearDown(self):
        _close_journals(self)

    def test_a_failed_cancel_whose_order_filled_becomes_a_position(self):
        t = make_trader()
        states = iter([m.OrderState("RESTING", 0.0, 0.0, None),
                       m.OrderState("FILLED", 5.0, 12.5, 0.40)])
        t._client.order_state = lambda oid: next(states)
        t._client.cancel_orders = lambda ids: ([], {"o1": "already filled"})
        rnd = make_round()
        t._client.now_ms = lambda: rnd.end_ms - 120_000
        t._pending["o1"] = make_pending("o1", expires_in_ms=-1, amount=5.0)
        t._reap_pending()
        self.assertEqual(len(t._positions), 1,
                         "a filled order was abandoned because the cancel "
                         "reported failure")
        self.assertNotIn("o1", t._pending)


class TestPaperRestingOrders(unittest.TestCase):
    """Paper and live share one lifecycle, or paper proves nothing."""

    def tearDown(self):
        _close_journals(self)

    def _book(self, asks=(), bids=()):
        class FakeMarketData:
            def asks(self, rnd, side):
                return list(asks) or None

            def bids(self, rnd, side):
                return list(bids) or None

        return m.PaperBook(FakeMarketData())

    def _buy(self, amount=5.0, price=0.40):
        return m.OrderPlan(side=Side.UP, action=m.Action.BUY,
                           order_type=m.OrderType.LIMIT, amount=amount,
                           price_limit=price)

    def _sell(self, amount=10.0, price=0.60):
        return m.OrderPlan(side=Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=amount,
                           price_limit=price)

    def test_a_bid_below_the_ask_does_not_fill(self):
        book = self._book(asks=[(0.50, 100.0)])
        oid = book.place(self._buy(), make_round())
        self.assertEqual(book.order_state(oid).status, "RESTING")

    def test_a_bid_at_or_above_the_ask_fills(self):
        book = self._book(asks=[(0.40, 100.0)])
        oid = book.place(self._buy(), make_round())
        state = book.order_state(oid)
        self.assertEqual(state.status, "FILLED")
        self.assertAlmostEqual(state.filled_usdt, 5.0)
        self.assertAlmostEqual(state.price, 0.40)

    def test_a_thin_book_fills_only_what_is_there(self):
        """Depth is the whole point: a partial fill has to be reachable."""
        book = self._book(asks=[(0.40, 5.0)])       # 5 shares = 2.0 USDT
        oid = book.place(self._buy(), make_round())
        state = book.order_state(oid)
        self.assertEqual(state.status, "PARTIAL")
        self.assertAlmostEqual(state.filled_usdt, 2.0)

    def test_a_sell_fills_against_the_bid_not_the_ask(self):
        book = self._book(asks=[(0.90, 100.0)], bids=[(0.60, 100.0)])
        oid = book.place(self._sell(), make_round())
        self.assertEqual(book.order_state(oid).status, "FILLED")

    def test_a_sell_above_the_bid_rests(self):
        book = self._book(bids=[(0.50, 100.0)])
        oid = book.place(self._sell(), make_round())
        self.assertEqual(book.order_state(oid).status, "RESTING")

    def test_a_cancelled_paper_order_is_dead_and_keeps_its_fill(self):
        book = self._book(asks=[(0.40, 5.0)])
        oid = book.place(self._buy(), make_round())
        book.cancel_orders([oid])
        state = book.order_state(oid)
        self.assertEqual(state.status, "DEAD")
        self.assertAlmostEqual(state.filled_usdt, 2.0)

    def test_an_unknown_paper_order_is_none(self):
        self.assertIsNone(self._book().order_state("nope"))

    def test_paper_mode_places_no_real_order(self):
        t = make_trader(live=False, entry_order_type="LIMIT")

        def boom(*a, **k):
            raise AssertionError("paper mode reached the venue")

        t._client.place_order = boom
        t._client.get_quote = boom
        rnd = make_round()
        t._post_limit_entry(rnd, make_signal(), 100.0, 0.5, 50.0, "PAPER",
                            rnd.end_ms)
        self.assertEqual(len(t._pending), 1)
        self.assertTrue(next(iter(t._pending)).startswith("paper-"))


class TestLimitExits(unittest.TestCase):
    """Selling is the only way out that does not wait for the oracle."""

    def tearDown(self):
        _close_journals(self)

    def _trader(self, **overrides):
        t = make_trader(live=False, **overrides)
        rnd = make_round()
        t._client.now_ms = lambda: rnd.end_ms - 120_000
        t._market_data.spot = lambda symbol: 100_000.0
        return t

    @staticmethod
    def _fills(trader, state):
        """Script what the paper book reports for the resting sell."""
        trader._paper_book.order_state = lambda oid: state

    def test_no_exit_is_posted_when_exits_are_off(self):
        t = self._trader(exit_order_type="NONE")
        t._positions[("BTCUSDT", Side.UP)] = make_position()
        t._maybe_exit_all()
        self.assertEqual(t._pending, {})

    def test_a_resting_exit_is_posted_once_the_position_exists(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", Side.UP)] = make_position()
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)
        pending = next(iter(t._pending.values()))
        self.assertIs(pending.plan.action, m.Action.SELL)
        self.assertIs(pending.plan.order_type, m.OrderType.LIMIT)

    def test_a_resting_exit_is_posted_only_once(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        t._positions[("BTCUSDT", Side.UP)] = make_position()
        t._maybe_exit_all()
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)

    def test_a_polled_exit_waits_for_the_bid_to_cross(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="POLLED")
        t._positions[("BTCUSDT", Side.UP)] = make_position()
        t._market_data.bids = lambda rnd, side: [(0.01, 100.0)]
        t._maybe_exit_all()
        self.assertEqual(t._pending, {})
        t._market_data.bids = lambda rnd, side: [(0.99, 100.0)]
        t._maybe_exit_all()
        self.assertEqual(len(t._pending), 1)

    def test_a_filled_sell_closes_the_row_from_its_proceeds(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        tid = t._journal.record("PAPER", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o1")
        pos = make_position(committed=5.0, trade_id=tid)
        t._positions[("BTCUSDT", Side.UP)] = pos
        t._maybe_exit_all()
        shares = pos.committed_usdt / pos.signal.fill_price
        self._fills(t, m.OrderState("FILLED", 6.0, shares, 0.60))
        t._reap_pending()
        self.assertEqual(t._positions, {})
        row = t._journal._conn.execute(
            "SELECT pnl, settle_source FROM trades WHERE id=?",
            (tid,)).fetchone()
        self.assertAlmostEqual(row[0], 1.0)
        self.assertEqual(row[1], "sold")

    def test_a_partial_sell_leaves_the_remainder_to_settle(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        tid = t._journal.record("PAPER", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o1")
        pos = make_position(committed=5.0, trade_id=tid)
        t._positions[("BTCUSDT", Side.UP)] = pos
        t._maybe_exit_all()
        shares = 0.4 * pos.committed_usdt / pos.signal.fill_price
        self._fills(t, m.OrderState("PARTIAL", 2.0, shares, 0.60))
        t._reap_pending()
        remaining = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(remaining.committed_usdt, 3.0)

    def test_selling_every_recorded_share_closes_the_position(self):
        """
        A fee-net holding sells for fewer shares than cost / price implies.
        Reckoned by cost, that full sale left a phantom remainder open; by
        the recorded count it is exactly what was held.
        """
        import dataclasses as _dc
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        tid = t._journal.record("PAPER", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o1")
        base = make_position(committed=5.0, trade_id=tid)
        held = 0.98 * base.committed_usdt / base.signal.fill_price
        t._positions[("BTCUSDT", Side.UP)] = _dc.replace(base, shares=held)
        t._maybe_exit_all()
        self._fills(t, m.OrderState("FILLED", 6.0, held, 0.60))
        t._reap_pending()
        self.assertEqual(t._positions, {})

    def test_a_partial_sale_counts_the_recorded_shares_down(self):
        import dataclasses as _dc
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        tid = t._journal.record("PAPER", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o1")
        base = make_position(committed=5.0, trade_id=tid)
        t._positions[("BTCUSDT", Side.UP)] = _dc.replace(base, shares=8.0)
        t._maybe_exit_all()
        self._fills(t, m.OrderState("PARTIAL", 2.0, 2.0, 0.60))
        t._reap_pending()
        left = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(left.shares, 6.0)
        self.assertAlmostEqual(left.committed_usdt, 3.75)

    def test_selling_more_than_is_held_is_refused(self):
        t = self._trader(exit_order_type="LIMIT", exit_trigger="RESTING")
        tid = t._journal.record("PAPER", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o1")
        t._positions[("BTCUSDT", Side.UP)] = make_position(committed=5.0,
                                                           trade_id=tid)
        t._maybe_exit_all()
        self._fills(t, m.OrderState("FILLED", 99.0, 200.0, 0.60))
        with self.assertLogs("btc5m", level="ERROR"):
            t._reap_pending()
        self.assertIn(("BTCUSDT", Side.UP), t._positions,
                      "an impossible sale was netted instead of refused")


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
        _close_journals(self)
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
        _close_journals(self)
        os.unlink(self.db)

    def _stub_trader(self, boom):
        """A Trader whose loop body raises `boom`, with startup stubbed out."""
        c = cfg(db_path=self.db, max_consecutive_errors=3,
                poll_interval_s=0.001, error_backoff_max_s=0.001)
        t = m.Trader.__new__(m.Trader)
        t._static_cfg = c
        t._store = None
        t._positions = {}
        t._pending = {}
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
        t._hydrated = {}

        class _Client:
            def sync_clock(self):
                return 0

            def now_ms(self):
                return 0

        t._client = _Client()
        # The loop brings the feeds up and points their
        # subscriptions at what is being traded. This stub never
        # gets past _maybe_enter, but run() touches the seam before
        # it reaches there.
        t._market_data = types.SimpleNamespace(
            start=lambda: None,
            stop=lambda: None,
            track=lambda symbols: None,
            status=lambda: {"spot": "off", "book": "off", "futures": "off"})
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


class TestSoldPositionsReachTheRiskManager(unittest.TestCase):
    """
    A sold position never reaches _settle_one, which is where every other
    ending reports itself. Without this the daily loss limit and the streak
    counter are wired to a path a selling profile never takes.
    """

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _sold(self, proceeds, **cfgkw):
        rnd = make_round()
        client = ScalpClient([rnd], [(rnd.end_ms - 200_000, 100_000.0)],
                             {}, {})
        c = scalp_cfg(db_path=self.db, **cfgkw)
        t = build_trader(client, c, self.db)
        self._built.append(t)
        key = ("BTCUSDT", Side.UP)
        sig = Signal(Side.UP, 0.5, 0.50, 0.0, 10.0, 200.0)
        tid = t._journal.record("PAPER", rnd, sig, 0.0, 0.0, 100.0)
        t._positions[key] = Position(tid, rnd, sig, 10.0, 1)
        t._brackets[key] = m.Bracket(0.50, 0.5357, 0.4847, "tp1")
        plan = m.OrderPlan(side=Side.UP, action=m.Action.SELL,
                           order_type=m.OrderType.LIMIT, amount=20.0,
                           price_limit=0.5357)
        pending = m.PendingOrder(order_id="tp1", rnd=rnd, plan=plan,
                                 signal=sig, expires_at_ms=rnd.end_ms,
                                 filled_usdt=0.0, filled_shares=0.0,
                                 trade_id=tid)
        t._pending["tp1"] = pending
        state = m.OrderState("FILLED", proceeds, 20.0, proceeds / 20.0)
        t._book_sale("tp1", pending, state)
        return t, key

    def test_a_winning_sale_is_reported_as_a_win(self):
        t, _ = self._sold(10.50)
        self.assertEqual(t._account_risk.consecutive_losses, 0)
        self.assertAlmostEqual(t._account_risk.realised_pnl, 0.50)

    def test_a_losing_sale_moves_the_streak_and_the_daily_total(self):
        t, _ = self._sold(9.50)
        self.assertEqual(t._account_risk.consecutive_losses, 1)
        self.assertAlmostEqual(t._account_risk.realised_pnl, -0.50)

    def test_the_per_symbol_manager_hears_it_too(self):
        t, _ = self._sold(9.50)
        self.assertEqual(t._risk["BTCUSDT"].consecutive_losses, 1)

    def test_the_round_trip_counts_toward_the_daily_ceiling(self):
        t, _ = self._sold(10.50)
        self.assertEqual(t._account_risk.rounds_today, 1)

    def test_the_paper_bankroll_actually_moves(self):
        """Without this a paper scalper trades all day against a fixed balance."""
        t, _ = self._sold(10.50)
        self.assertAlmostEqual(t._paper_bankroll,
                               scalp_cfg().paper_start_bankroll + 0.50)

    def test_calibration_is_not_fed_an_outcome_that_never_happened(self):
        """
        Those buckets answer "did the price paid predict the outcome", and a
        position closed BEFORE the outcome existed has no answer to give.
        """
        t, _ = self._sold(10.50)
        self.assertIsNone(t._account_risk.calibration_z())

    def test_the_bracket_leaves_with_the_position(self):
        t, key = self._sold(10.50)
        self.assertNotIn(key, t._positions)
        self.assertNotIn(key, t._brackets)


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
            _close_journals()
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
