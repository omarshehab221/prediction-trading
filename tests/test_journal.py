"""The record of what was traded, and every report read off it."""

from __future__ import annotations

import os
import tempfile
import unittest

import btc_5m_predictor as m
from btc_5m_predictor import (
    Journal,
    Position,
    Side,
    Signal,
    breakeven_probability,
    settle_pnl,
)
from tests.support import (
    FakeClient,
    _close_journals,
    build_trader,
    cfg,
    make_round,
    make_signal,
)

class TestJournal(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.j = Journal(self.db)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def test_empty_report(self):
        self.assertIn("No resolved trades", self.j.calibration_report())

    def test_new_columns_exist(self):
        cols = {r[1] for r in
                self.j._conn.execute("PRAGMA table_info(trades)")}
        for col in ("order_type", "price_limit", "exit_price",
                    "exit_order_id"):
            self.assertIn(col, cols, col)

    def test_a_journal_without_the_columns_is_still_readable(self):
        """A migration that drops old journals discards the calibration record."""
        import sqlite3 as _s
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        conn = _s.connect(path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, ts INTEGER)")
        conn.commit()
        conn.close()
        j = Journal(path, "p")
        cols = {r[1] for r in j._conn.execute("PRAGMA table_info(trades)")}
        self.assertIn("order_type", cols)

    def test_resolve_sold_records_proceeds_and_its_own_source(self):
        tid = self.j.record("LIVE", make_round(), make_signal(), 100.0, 0.5,
                            50.0, "o1")
        self.j.resolve_sold(tid, proceeds_usdt=6.0, exit_price=0.60,
                            exit_order_id="o2", stake=5.0)
        row = self.j._conn.execute(
            "SELECT resolved, pnl, settle_source, exit_price, exit_order_id"
            " FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], 1)
        self.assertAlmostEqual(row[1], 1.0)      # 6.0 proceeds - 5.0 stake
        self.assertEqual(row[2], "sold")
        self.assertAlmostEqual(row[3], 0.60)
        self.assertEqual(row[4], "o2")

    def test_sold_trades_are_excluded_from_the_calibration_buckets(self):
        """
        The buckets ask whether the price paid predicted the outcome. A trade
        closed before the outcome existed has no answer, and counting it as
        one corrupts the only number this bot exists to produce.
        """
        for _ in range(30):
            tid = self.j.record("LIVE", make_round(), make_signal(), 100.0,
                                0.5, 50.0, "o")
            self.j.resolve(tid, True, 1.0, "venue")
        sold = self.j.record("LIVE", make_round(), make_signal(), 100.0, 0.5,
                             50.0, "o")
        self.j.resolve_sold(sold, proceeds_usdt=99.0, exit_price=0.99,
                            exit_order_id="x", stake=1.0)
        report = self.j.diagnose()
        self.assertIn("Trades analysed : 30", report)
        self.assertIn("sold before settlement", report)

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


class TestBiasReport(unittest.TestCase):
    """The report must measure which side of the market is mispriced."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.j = Journal(self.db)

    def tearDown(self):
        _close_journals(self)
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


class TestPerProfileReport(unittest.TestCase):
    """Pooling profiles averages disjoint price bands into a meaningless bias."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestBufferReport(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestDiagnose(unittest.TestCase):
    """Is the edge real? The one question that decides everything."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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


class TestPerMarketFeeInDiagnostics(unittest.TestCase):
    """The breakeven bar must use each market's own published fee."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd)

    def tearDown(self):
        _close_journals(self)
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
            _close_journals()
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
            _close_journals()
            os.unlink(db)
