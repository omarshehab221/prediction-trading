"""The four strategies, entered end to end against a fake venue."""

from __future__ import annotations

import os
import tempfile
import unittest

import btc_5m_predictor as m
from btc_5m_predictor import Position, Side, Signal, breakeven_probability
from tests.support import (
    FakeClient,
    QuotingClient,
    ScalpClient,
    _close_journals,
    build_trader,
    cfg,
    lastminute_cfg,
    make_pending,
    make_round,
    scalp_cfg,
    straddle_cfg,
)

class TestStraddleEntry(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
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

        Switched on explicitly: the straddle profile ships it OFF, because at
        the price that profile opens at the deadline hedge costs more than
        the bet is worth. This covers the mechanism, not the default.
        """
        start = 1_700_000_000_000
        end = start + (m.DEFAULT_ROUND_SECONDS * 1000)
        rnd = make_round(strike=100_000.0, start_ms=start, end_ms=end,
                         fee_bps=0)
        books = {(1, Side.UP): [(0.25, 10_000)],
                 (1, Side.DOWN): [(0.90, 10_000)]}
        client = FakeClient([rnd], [(start + 3_000, 100_000.0),
                                    (end - 10_000, 100_000.0)], books, {})
        t = self._trader(client, straddle_force_hedge=True)

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
        _close_journals(self)
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


class TestLastMinuteEntry(unittest.TestCase):
    """The whole strategy: dearer side, floor, fallback, and nothing else."""

    START = 1_700_000_000_000

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
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


class TestScalpSignal(unittest.TestCase):
    """Both conditions, or no trade."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _trader(self, *, move=None, age=0.0, perp=None, spot=100_000.0,
                **cfgkw):
        rnd = make_round()
        client = ScalpClient([rnd], [(rnd.end_ms - 200_000, spot)], {}, {})
        # These test the spot-lag gate itself, so they switch it on; the
        # profile trades the futures move alone (scalp_min_basis_bps 0).
        cfgkw.setdefault("scalp_min_basis_bps", 0.5)
        t = build_trader(client, scalp_cfg(db_path=self.db, **cfgkw), self.db)
        self._built.append(t)
        t._market_data.futures_move_bps = lambda symbol, lookback: move
        t._market_data.futures_tick_age_ms = lambda symbol: age
        t._market_data.futures_mid = lambda symbol: perp
        return t

    def _warm(self, t, symbol="BTCUSDT", basis_bps=0.0):
        """Fill the basis EWMA so a dislocation can be measured off it."""
        t._basis_ewma[symbol] = (basis_bps, m.BASIS_EWMA_MIN_SAMPLES)

    def test_no_feed_is_no_signal(self):
        t = self._trader(move=None)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_a_stale_tick_is_not_a_signal(self):
        """
        Per symbol, because the socket's own health flag asks about ANY
        frame across every subscription -- a busy market keeps it green
        while a quiet one's newest tick is a minute old.
        """
        t = self._trader(move=50.0, age=9_000.0)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_a_move_under_the_floor_is_not_a_signal(self):
        t = self._trader(move=0.5, scalp_min_basis_bps=0.0)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_momentum_alone_trades_when_the_basis_gate_is_off(self):
        t = self._trader(move=5.0, scalp_min_basis_bps=0.0)
        side, move, dislocation = t._scalp_signal("BTCUSDT")
        self.assertIs(side, Side.UP)
        self.assertEqual(move, 5.0)
        self.assertEqual(dislocation, 0.0)

    def test_a_falling_perp_points_down(self):
        t = self._trader(move=-5.0, scalp_min_basis_bps=0.0)
        side, _, _ = t._scalp_signal("BTCUSDT")
        self.assertIs(side, Side.DOWN)

    def test_a_move_spot_has_already_followed_is_refused(self):
        """
        The perp is up but the basis has not richened, so spot is level with
        it -- there is nothing left to be early to.
        """
        t = self._trader(move=5.0, perp=100_000.0, spot=100_000.0)
        self._warm(t)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_a_move_spot_has_not_followed_is_taken(self):
        # Perp 2 bps above spot against an EWMA mean of 0: a 2 bps
        # dislocation, over the 0.5 bps floor.
        t = self._trader(move=5.0, perp=100_020.0, spot=100_000.0)
        self._warm(t)
        signal = t._scalp_signal("BTCUSDT")
        self.assertIsNotNone(signal)
        side, _, dislocation = signal
        self.assertIs(side, Side.UP)
        self.assertAlmostEqual(dislocation, 2.0, places=6)

    def test_a_dislocation_pointing_the_other_way_is_refused(self):
        """Perp up, basis cheapening: spot has already overtaken it."""
        t = self._trader(move=5.0, perp=99_980.0, spot=100_000.0)
        self._warm(t)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_the_basis_level_is_not_the_signal(self):
        """
        A persistent 30 bps basis is funding, not information, so once the
        mean has learned it a reading of 30 bps says nothing.
        """
        t = self._trader(move=5.0, perp=100_300.0, spot=100_000.0)
        self._warm(t, basis_bps=30.0)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))

    def test_the_first_observations_are_not_compared_against_themselves(self):
        t = self._trader(move=5.0, perp=100_020.0, spot=100_000.0)
        self.assertIsNone(t._scalp_signal("BTCUSDT"))
        self.assertEqual(t._basis_ewma["BTCUSDT"][1], 1)

    def test_the_mean_is_advanced_but_not_used_on_the_same_sample(self):
        t = self._trader(move=5.0, perp=100_020.0, spot=100_000.0)
        self._warm(t)
        t._scalp_signal("BTCUSDT")
        mean, seen = t._basis_ewma["BTCUSDT"]
        self.assertEqual(seen, m.BASIS_EWMA_MIN_SAMPLES + 1)
        self.assertAlmostEqual(mean, 2.0 * m.BASIS_EWMA_ALPHA, places=9)


class TestScalpEntry(unittest.TestCase):
    """Entry, the bracket it arms, and the three bounds on repeating."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _trader(self, *, secs_left=200.0, ask=0.50, bid=0.49, live=False,
                **cfgkw):
        rnd = make_round()
        now = rnd.end_ms - int(secs_left * 1000)
        books = {(1, Side.UP): [(ask, 10_000)], (1, Side.DOWN): [(ask, 10_000)]}
        client = ScalpClient([rnd], [(now, 100_000.0)], books, {},
                             bids={(1, Side.UP): [(bid, 10_000)],
                                   (1, Side.DOWN): [(bid, 10_000)]})
        c = scalp_cfg(db_path=self.db, live=live, **cfgkw)
        t = build_trader(client, c, self.db)
        self._built.append(t)
        # A clean, confirmed signal unless a test says otherwise.
        t._market_data.futures_move_bps = lambda symbol, lookback: 5.0
        t._market_data.futures_tick_age_ms = lambda symbol: 0.0
        t._market_data.futures_mid = lambda symbol: 100_020.0
        t._basis_ewma["BTCUSDT"] = (0.0, m.BASIS_EWMA_MIN_SAMPLES)
        return t, client

    def test_dispatch_uses_the_scalp_path_when_enabled(self):
        t, _ = self._trader()
        called = {"scalp": False, "model": False, "last": False}
        t._maybe_enter_scalp = lambda *a: called.__setitem__("scalp", True)
        t._maybe_enter_model = lambda *a: called.__setitem__("model", True)
        t._maybe_enter_last_minute = lambda *a: called.__setitem__("last", True)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(called, {"scalp": True, "model": False,
                                  "last": False})

    def test_a_confirmed_lead_opens_a_position(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_a_bankroll_the_reserve_leaves_under_the_minimum_is_explained(self):
        """
        Live at 1.19 USDT the 30% reserve left 0.83 free, the entry returned
        on its first check, and the only word of it was at DEBUG -- a quarter
        of an hour of silence that read exactly like a hung bot.
        """
        t, _ = self._trader()
        with self.assertLogs("btc5m", level="INFO") as logs:
            t._maybe_enter(1.19, "PAPER")
        self.assertEqual(t._positions, {})
        text = "\n".join(logs.output)
        self.assertIn("WAITING scalp", text)
        self.assertIn("0.83", text)
        self.assertIn("reserve", text)

    def test_an_unchanged_reason_is_not_repeated_every_pass(self):
        t, _ = self._trader(decision_log_interval_s=3600.0)
        with self.assertLogs("btc5m", level="INFO") as logs:
            for _ in range(5):
                t._maybe_enter(1.19, "PAPER")
            t._explain("scalp", "something else", "a new reason")
        waiting = [line for line in logs.output if "WAITING" in line]
        self.assertEqual(len(waiting), 2, waiting)

    def test_a_missing_lead_says_what_the_perp_did(self):
        t, _ = self._trader()
        t._market_data.futures_move_bps = lambda s, l: 0.4
        with self.assertLogs("btc5m", level="INFO") as logs:
            t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertIn("perp moved +0.40bp", "\n".join(logs.output))

    def test_the_side_follows_the_perp(self):
        t, _ = self._trader()
        t._market_data.futures_move_bps = lambda s, l: -5.0
        t._market_data.futures_mid = lambda s: 99_980.0
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.DOWN), t._positions)

    def test_the_basis_mean_warms_up_on_quiet_passes(self):
        """
        A fresh trader -- every restart -- has no basis mean. It must learn
        one while the perp is quiet, so the first real move can trade.
        Sampled only on moves, the first twenty moves after each restart
        were spent warming up and none of them traded.
        """
        t, _ = self._trader()
        t._basis_ewma.clear()
        t._market_data.futures_move_bps = lambda s, l: 0.0
        t._market_data.futures_mid = lambda s: 100_000.0
        for _ in range(m.BASIS_EWMA_MIN_SAMPLES):
            t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        t._market_data.futures_move_bps = lambda s, l: 5.0
        t._market_data.futures_mid = lambda s: 100_020.0
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_an_entry_whose_bid_is_already_under_the_stop_is_skipped(self):
        """
        The entry buys the ask; the stop watches the bid. When the spread is
        wider than the stop distance the position is stopped the moment it
        opens -- live, BNB UP bought 0.80 with the bid at 0.61 stopped out
        three seconds later. Nothing is entered that is born stopped.
        """
        t, _ = self._trader(ask=0.50, bid=0.40)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_an_entry_whose_bid_clears_the_stop_still_trades(self):
        t, _ = self._trader(ask=0.50, bid=0.49)
        t._maybe_enter(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_no_signal_opens_nothing(self):
        t, _ = self._trader()
        t._market_data.futures_move_bps = lambda s, l: None
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_bracket_is_armed_at_the_prices_the_fill_implies(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        bracket = t._brackets[("BTCUSDT", Side.UP)]
        expected = m.bracket_prices(0.50, 200, 0.05, 0.05)
        self.assertAlmostEqual(bracket.tp_price, round(expected[0], 4))
        self.assertAlmostEqual(bracket.stop_price, expected[1])
        self.assertAlmostEqual(bracket.entry_price, 0.50)

    def test_the_take_profit_is_watched_not_rested(self):
        """
        Nothing rests on the book. A resting take-profit had to be cancelled
        before a stop could sell, and batch-cancel has never succeeded on
        this venue -- every stop then waited on a cancel that could not
        happen while the price kept falling.
        """
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        bracket = t._brackets[("BTCUSDT", Side.UP)]
        self.assertFalse([p for p in t._pending.values()
                          if p.plan.action is m.Action.SELL])

    def test_the_stop_is_never_an_order(self):
        """
        A SELL limit below the bid is marketable: posting one would close the
        position at once rather than wait for the price to fall to it.
        """
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        bracket = t._brackets[("BTCUSDT", Side.UP)]
        resting = [p.plan.price_limit for p in t._pending.values()]
        self.assertNotIn(bracket.stop_price, resting)
        self.assertEqual(resting, [])

    def test_the_entry_crosses_rather_than_rests(self):
        # Live fills at the quote's 0.51, putting the stop near 0.494;
        # the bid has to clear it or the spread gate refuses the entry.
        t, client = self._trader(live=True, bid=0.50)
        t._maybe_enter(100.0, "PAPER")
        entry = client.orders[0]
        self.assertIs(entry[1], m.Action.BUY)
        self.assertIsNone(entry[3])        # no price limit: it is a taker

    def test_the_round_is_never_marked_seen(self):
        """
        Writing _seen is what caps every other strategy at one entry per
        round, and repeating inside one round is the whole profile.
        """
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._seen, {})
        self.assertEqual(t._scalp_entries[1][1], 1)

    def test_a_symbol_that_is_not_flat_is_left_alone(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        before = dict(t._positions)
        t._scalp_last_entry.clear()
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions.keys(), before.keys())
        self.assertEqual(t._scalp_entries[1][1], 1)

    def test_a_resting_order_also_blocks_a_new_entry(self):
        t, _ = self._trader()
        t._pending["x"] = make_pending("x", expires_in_ms=60_000)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_cooldown_stops_one_signal_becoming_three_positions(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        t._positions.clear()
        t._pending.clear()
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_round_ceiling_binds(self):
        t, _ = self._trader(scalp_max_entries_per_round=2)
        for _ in range(4):
            t._positions.clear()
            t._pending.clear()
            t._scalp_last_entry.clear()
            t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._scalp_entries[1][1], 2)

    def test_entries_stop_at_the_window_edge(self):
        t, _ = self._trader(secs_left=70.0)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_a_fee_that_demands_too_much_signal_is_refused(self):
        """The gate the whole profile's economics hang on."""
        rnd = make_round(fee_bps=400)
        now = rnd.end_ms - 200_000
        client = ScalpClient([rnd], [(now, 100_000.0)],
                             {(1, Side.UP): [(0.50, 10_000)]}, {})
        t = build_trader(client, scalp_cfg(db_path=self.db), self.db)
        self._built.append(t)
        t._market_data.futures_move_bps = lambda s, l: 5.0
        t._market_data.futures_tick_age_ms = lambda s: 0.0
        t._basis_ewma["BTCUSDT"] = (0.0, m.BASIS_EWMA_MIN_SAMPLES)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertIn("fee", t._watching[1][1])

    def test_a_price_with_no_room_for_a_target_is_refused(self):
        """
        The band already stops short of here, so this widens it on purpose:
        the gate has to be the bracket's own arithmetic, not the band that
        happens to sit in front of it.
        """
        t, _ = self._trader(ask=0.94, max_entry_price=0.95)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        self.assertIn("bracket", t._watching[1][1])

    def test_the_profiles_own_band_never_reaches_that_price(self):
        """The gate above is a backstop; the band is what usually binds."""
        c = scalp_cfg()
        self.assertIsNotNone(
            m.bracket_prices(c.max_entry_price, 200,
                             c.scalp_take_profit_pct, c.scalp_stop_loss_pct))

    def test_a_thin_book_is_refused(self):
        rnd = make_round(liquidity=10.0)
        now = rnd.end_ms - 200_000
        client = ScalpClient([rnd], [(now, 100_000.0)],
                             {(1, Side.UP): [(0.50, 10_000)]}, {})
        t = build_trader(client, scalp_cfg(db_path=self.db), self.db)
        self._built.append(t)
        t._market_data.futures_move_bps = lambda s, l: 5.0
        t._market_data.futures_tick_age_ms = lambda s: 0.0
        t._basis_ewma["BTCUSDT"] = (0.0, m.BASIS_EWMA_MIN_SAMPLES)
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_the_journal_records_the_price_paid_not_a_forecast(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        pos = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(pos.signal.model_prob,
                               breakeven_probability(0.50, 200))
        self.assertEqual(pos.signal.edge, 0.0)

    def test_a_live_entry_records_the_shares_the_quote_returned(self):
        """The count every exit sells comes from the venue, not from cost."""
        import dataclasses as _dc
        # Live fills at the quote's 0.51, putting the stop near 0.494;
        # the bid has to clear it or the spread gate refuses the entry.
        t, client = self._trader(live=True, bid=0.50)
        quote = client.get_quote

        def net_of_fee(rnd, plan):
            q = quote(rnd, plan)
            if plan.action is m.Action.BUY:
                q = _dc.replace(q, amount_out=q.amount_out * 0.98)
            return q

        client.get_quote = net_of_fee
        t._maybe_enter(100.0, "LIVE")
        pos = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(pos.shares,
                               pos.committed_usdt / 0.51 * 0.98, places=6)

    def test_a_live_entry_records_the_shares_the_venue_delivered(self):
        """
        Live, the quote said 2.294 shares and the venue delivered 2.29: it
        holds shares to two decimals. The stop's sale of 2.294 was refused as
        exceeding the shares available, and so was the flatten, and the
        position ran to settlement. The delivered count wins whenever the
        venue reports one.
        """
        t, client = self._trader(live=True, bid=0.50)
        client.delivered_shares = lambda order_id: 1.23
        t._maybe_enter(100.0, "LIVE")
        pos = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(pos.shares, 1.23)

    def test_a_live_entry_is_priced_and_bracketed_at_the_executed_price(self):
        """
        Live, a BTC buy quoted at 0.49 executed at 0.36 (0.73 USDT for 2
        shares; the venue balance fell by exactly 0.73). The bot recorded the
        quote's 0.49 and bracketed it: the stop landed at 0.475, above the
        real entry, and fired on a position that was up. Entry price, P&L and
        bracket all come from what the venue executed.
        """
        t, client = self._trader(live=True, bid=0.50)
        client.executed_price = lambda order_id: 0.36
        t._maybe_enter(100.0, "LIVE")
        key = ("BTCUSDT", Side.UP)
        pos = t._positions[key]
        self.assertAlmostEqual(pos.signal.fill_price, 0.36)
        bracket = t._brackets[key]
        self.assertAlmostEqual(bracket.entry_price, 0.36)
        self.assertLess(bracket.stop_price, 0.36)

    def test_a_live_entry_without_an_executed_price_keeps_the_quote(self):
        t, client = self._trader(live=True, bid=0.50)
        t._maybe_enter(100.0, "LIVE")
        pos = t._positions[("BTCUSDT", Side.UP)]
        self.assertAlmostEqual(pos.signal.fill_price, 0.51)

    # -- a quote the book does not support is not traded -------------------

    def _quoting(self, client, avg):
        """Every buy quote comes back at `avg`, whatever the book says."""
        def quote(rnd, plan):
            return m.Quote("q-far", avg, plan.amount / avg, 0.0, 0.0,
                           action=plan.action, order_type=plan.order_type,
                           price_limit=plan.price_limit)
        client.get_quote = quote

    def _buys(self, client):
        return [o for o in client.orders if o[1] is m.Action.BUY]

    def test_a_quote_far_below_the_book_is_not_traded(self):
        """
        Live, an ETH UP buy was quoted and sent at 0.39 while the book asked
        0.60; the venue answered "Failed to execute the market order", and
        five seconds later the same side filled at 0.59. Half the session's
        buys failed like that, and one that did fill executed at 0.36 under
        a 0.43 bid, which put its stop above the real entry. A quote further
        from the book's ask than the order's own slippage cap is skipped.
        """
        t, client = self._trader(live=True, ask=0.50, bid=0.49)
        self._quoting(client, 0.39)
        with self.assertLogs(level="WARNING") as logs:
            t._maybe_enter(100.0, "LIVE")
        self.assertNotIn(("BTCUSDT", Side.UP), t._positions)
        self.assertEqual(self._buys(client), [])
        text = "\n".join(logs.output)
        self.assertIn("0.3900", text)
        self.assertIn("0.5000", text)

    def test_a_quote_far_above_the_book_is_not_traded(self):
        # A bid high enough that the spread-wider-than-the-stop gate passes:
        # only the distance from the ask may stop this one.
        t, client = self._trader(live=True, ask=0.50, bid=0.59)
        self._quoting(client, 0.60)
        t._maybe_enter(100.0, "LIVE")
        self.assertNotIn(("BTCUSDT", Side.UP), t._positions)
        self.assertEqual(self._buys(client), [])

    def test_a_failed_buy_logs_its_quote_and_the_book(self):
        """
        The venue's record of a failed buy says only "Failed to execute the
        market order". What the bot was quoted and what its book showed at
        that moment are the two numbers that say which side was wrong.
        """
        t, client = self._trader(live=True, ask=0.50, bid=0.50)

        def killed(order_id, requested_usdt):
            raise m.OrderNotFilled(f"order {order_id} did not fill: status "
                                   f"FAILED, filled 0.0")

        client.confirm_fill = killed
        with self.assertLogs(level="WARNING") as logs:
            t._maybe_enter(100.0, "LIVE")
        self.assertNotIn(("BTCUSDT", Side.UP), t._positions)
        text = "\n".join(logs.output)
        self.assertIn("quote 0.5100", text)
        self.assertIn("ask 0.5000", text)
        self.assertIn("bid 0.5000", text)

    # -- the book an entry is screened on ------------------------------------

    def test_a_crossed_book_is_not_traded(self):
        """
        Live logged books the venue never had -- ask 0.63 against bid 0.65,
        ask 0.54 against bid 0.59 -- and every entry gate reads that touch.
        A bid over the ask is not a price anyone can trade, so the entry is
        skipped and both sources are logged.
        """
        t, client = self._trader(live=True, ask=0.50, bid=0.55)
        with self.assertLogs(level="WARNING") as logs:
            t._maybe_enter(100.0, "LIVE")
        self.assertNotIn(("BTCUSDT", Side.UP), t._positions)
        self.assertEqual(self._buys(client), [])
        self.assertIn("crossed", "\n".join(logs.output))

    def test_entry_is_screened_on_one_rest_snapshot_not_the_stream(self):
        # A stale stream bid far under the stop would refuse this entry if
        # the spread gate still read it; the snapshot's bid clears the stop.
        t, client = self._trader(live=True, ask=0.50, bid=0.50)
        t._market_data.bids = lambda rnd, side: [(0.10, 100.0)]
        t._maybe_enter(100.0, "LIVE")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    # -- sizing: the fraction, floored at the venue minimum ---------------

    def test_the_stake_is_the_fraction_when_it_clears_the_minimum(self):
        t, _ = self._trader(scalp_stake_pct=0.10, reserve_pct=0.0)
        t._maybe_enter(100.0, "PAPER")
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.stake_usdt, 10.0)

    def test_a_fraction_under_the_minimum_trades_the_minimum(self):
        """
        10% of 4.24 is 0.42, under the 1.00 venue minimum. A small account
        still takes one minimum-size order rather than sitting out every
        signal it is given.
        """
        t, _ = self._trader(scalp_stake_pct=0.10, reserve_pct=0.0)
        t._maybe_enter(4.24, "PAPER")
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].signal.stake_usdt,
            t._cfg.min_stake_usdt)

    def test_the_minimum_is_never_forced_past_what_is_free(self):
        """The floor raises a small stake; it never spends money held back."""
        t, _ = self._trader(scalp_stake_pct=0.10, reserve_pct=0.5)
        t._maybe_enter(1.50, "PAPER")        # 0.75 free, under the 1.00 floor
        self.assertEqual(t._positions, {})

    def test_scale_in_never_runs_on_a_scalp(self):
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        t._maybe_scale_in = lambda *a, **k: self.fail("scaled into a scalp")
        t._maybe_scale_in_all(100.0)

    def test_the_model_priced_exit_never_runs_on_a_scalp(self):
        """BRACKET is not a flavour of LIMIT; stacking would double-offer."""
        t, _ = self._trader()
        t._maybe_enter(100.0, "PAPER")
        t._post_exit = lambda pos: self.fail("offered a bracketed position")
        t._maybe_exit_all()


class TestScalpStops(unittest.TestCase):
    """The leg that is a trigger rather than an order."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _armed(self, bid, shares=None, **cfgkw):
        """A trader holding one bracketed position, with `bid` on the book."""
        rnd = make_round()
        now = rnd.end_ms - 200_000
        client = ScalpClient([rnd], [(now, 100_000.0)],
                             {(1, Side.UP): [(0.50, 10_000)]}, {},
                             bids={(1, Side.UP): [(bid, 10_000)]})
        # LIVE, so the orders go to the client rather than to the paper
        # book: these tests are about which orders reach the venue and in
        # what order, which paper mode answers about itself.
        c = scalp_cfg(db_path=self.db, live=True, **cfgkw)
        t = build_trader(client, c, self.db)
        self._built.append(t)
        sig = Signal(Side.UP, 0.5, 0.50, 0.0, 10.0, 200.0)
        tid = t._journal.record("PAPER", rnd, sig, 0.0, 0.0, 100.0)
        key = ("BTCUSDT", Side.UP)
        t._positions[key] = Position(tid, rnd, sig, 10.0, 1, shares=shares)
        # Every order plan the venue is asked to quote, so a test can read
        # how many shares each sale asked for.
        client.plans = []
        quote = client.get_quote

        def recording(r, plan):
            client.plans.append(plan)
            return quote(r, plan)

        client.get_quote = recording
        tp, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t._arm_bracket(key, 0.50, tp, stop)
        return t, client, key

    def _sells(self, client):
        return [p for p in client.plans if p.action is m.Action.SELL]

    def test_the_take_profit_sells_the_shares_the_buy_returned(self):
        """
        10.00 at 0.50 is 20 shares on paper and fewer in the wallet: the
        buy's fee comes out of the shares received. Offering 20 is what the
        venue refused as "exceeded your available shares".
        """
        tp, _ = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, _ = self._armed(tp, shares=19.6)
        t._check_stops()
        # Sized just under the count, for the reason the tests below give.
        self.assertAlmostEqual(self._sells(client)[-1].amount, 19.59)

    def test_a_stop_sells_the_shares_held(self):
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, _ = self._armed(stop, shares=19.6)
        t._check_stops()
        self.assertAlmostEqual(self._sells(client)[-1].amount, 19.59)

    def test_without_a_recorded_count_the_cost_implied_shares_are_sold(self):
        tp, _ = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, _ = self._armed(tp)
        t._check_stops()
        self.assertAlmostEqual(self._sells(client)[-1].amount, 19.99)

    # -- sizing a sale the venue will not refuse ----------------------------

    def test_a_sale_is_sized_under_a_count_the_venue_rounded_up(self):
        """
        Live, a BTC buy was quoted 1.447444 shares and its order record said
        filledShareQty 1.45 -- two decimals, rounded UP past what was held.
        The stop's sale of 1.45 was refused with -9000 "exceeded your
        available shares", so was the flatten, and the stake settled as a
        full loss. Every sale is sized to the share precision with half a
        unit taken off first, so a count rounded up by up to 0.005 still
        sells.
        """
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, _ = self._armed(stop, shares=1.45)
        t._check_stops()
        self.assertAlmostEqual(self._sells(client)[-1].amount, 1.44)

    def test_a_quote_count_above_the_holding_is_sized_under_it(self):
        # Live: sold the quote's 2.294 against a record of 2.29; refused.
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, _ = self._armed(stop, shares=2.294)
        t._check_stops()
        self.assertAlmostEqual(self._sells(client)[-1].amount, 2.28)

    def _refuse_sells(self, client, times):
        """The venue refuses the next `times` sale quotes with -9000."""
        refused = []
        base = client.get_quote

        def refusing(r, plan):
            if plan.action is m.Action.SELL and len(refused) < times:
                refused.append(plan.amount)
                client.plans.append(plan)
                raise m.ApiError("You have exceeded your available shares",
                                 code=-9000)
            return base(r, plan)

        client.get_quote = refusing
        return refused

    def test_a_refused_sale_is_retried_once_a_unit_lower(self):
        """
        A holding can sit further under the recorded count than the margin
        allows for. One refusal steps the sale down a unit and asks again,
        rather than leaving the position to run to settlement.
        """
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(stop, shares=1.45)
        self._refuse_sells(client, 1)
        t._check_stops()
        amounts = [p.amount for p in self._sells(client)]
        self.assertEqual(len(amounts), 2)
        self.assertAlmostEqual(amounts[0], 1.44)
        self.assertAlmostEqual(amounts[1], 1.43)
        self.assertTrue([p for p in t._pending.values()
                         if p.plan.action is m.Action.SELL])

    def test_a_sale_refused_twice_gives_up_and_reports_it(self):
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(stop, shares=1.45)
        self._refuse_sells(client, 2)
        pos = t._positions[key]
        self.assertFalse(t._sell_now(pos, "stop"))
        self.assertEqual(len(self._sells(client)), 2)
        self.assertFalse([p for p in t._pending.values()
                          if p.plan.action is m.Action.SELL])

    def test_a_refusal_for_another_reason_is_not_retried(self):
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(stop, shares=1.45)
        base = client.get_quote

        def malformed(r, plan):
            client.plans.append(plan)
            raise m.ApiError("malformed", code=-1102)

        client.get_quote = malformed
        self.assertFalse(t._sell_now(t._positions[key], "stop"))
        self.assertEqual(len(self._sells(client)), 1)

    def test_a_count_too_small_to_size_down_asks_for_nothing(self):
        t, client, key = self._armed(0.50, shares=0.004)
        self.assertFalse(t._sell_now(t._positions[key], "stop"))
        self.assertEqual(self._sells(client), [])

    def test_a_fired_stop_logs_the_rest_bid_beside_the_stream_bid(self):
        """
        Stops fire on the stream's bid. Whether that bid was real is the
        question the crossed books raised, so every trigger records a REST
        bid read at the same moment.
        """
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(stop)
        with self.assertLogs(level="INFO") as logs:
            t._check_stops()
        text = "\n".join(logs.output)
        self.assertIn("stream bid", text)
        self.assertIn("REST bid", text)

    def test_a_bid_at_the_take_profit_sells_the_position(self):
        tp, _ = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(tp)
        t._check_stops()
        self.assertNotIn(key, t._brackets)
        self.assertEqual(client.cancelled, [])
        self.assertTrue([p for p in t._pending.values()
                         if p.plan.action is m.Action.SELL])

    def test_a_bid_above_the_stop_does_nothing(self):
        t, client, key = self._armed(0.50)
        t._check_stops()
        self.assertIn(key, t._brackets)
        self.assertEqual(client.cancelled, [])

    def test_a_bid_at_the_stop_sells_without_cancelling_anything(self):
        _, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t, client, key = self._armed(stop)
        t._check_stops()
        self.assertEqual(client.cancelled, [])
        self.assertNotIn(key, t._brackets)
        self.assertTrue([p for p in t._pending.values()
                         if p.plan.action is m.Action.SELL])

    def test_the_stop_sells_through_the_bid_rather_than_resting(self):
        """
        A SELL priced AT the bid queues behind it. The point of a stop is to
        be out, so it reaches through by the profile's own slippage cap.
        """
        t, client, key = self._armed(0.40)
        t._check_stops()
        sale = [p for p in t._pending.values()
                if p.plan.action is m.Action.SELL]
        self.assertEqual(len(sale), 1)
        self.assertLess(sale[0].plan.price_limit, 0.40)

    def test_the_stop_uses_a_limit_not_a_fill_or_kill_market_order(self):
        """
        A MARKET order is FOK here: on the thin book that triggered the stop
        it fills entirely or not at all, and "not at all" is the capped
        position running to settlement uncapped.
        """
        t, _, key = self._armed(0.40)
        t._check_stops()
        sale = next(p for p in t._pending.values()
                    if p.plan.action is m.Action.SELL)
        self.assertIs(sale.plan.order_type, m.OrderType.LIMIT)

    def test_a_stop_sells_even_when_cancel_is_unavailable(self):
        """
        The live failure: batch-cancel refused every call, and the stop sat
        waiting on it while the bid fell from 0.61 to 0.57. With nothing
        resting, a stop never needs a cancel.
        """
        t, client, key = self._armed(0.40)

        def refuse(_ids):
            raise m.ApiError("cancel unavailable")

        t._client.cancel_orders = refuse
        t._check_stops()
        self.assertNotIn(key, t._brackets)
        self.assertTrue([p for p in t._pending.values()
                         if p.plan.action is m.Action.SELL])

    def test_a_bracket_whose_position_is_gone_is_forgotten(self):
        t, _, key = self._armed(0.50)
        t._positions.pop(key)
        t._check_stops()
        self.assertEqual(t._brackets, {})

    def test_nothing_happens_when_the_profile_is_off(self):
        t, client, key = self._armed(0.40, )
        t._static_cfg = cfg(db_path=self.db)
        t._check_stops()
        self.assertEqual(client.cancelled, [])


class TestScalpFlatten(unittest.TestCase):
    """The last minute belongs to nobody."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._built = []

    def tearDown(self):
        _close_journals(self)
        for trader in self._built:
            trader._journal._conn.close()
        os.unlink(self.db)

    def _at(self, secs_left, *, bid=0.50, **cfgkw):
        rnd = make_round()
        now = rnd.end_ms - int(secs_left * 1000)
        bids = {(1, Side.UP): [(bid, 10_000)]} if bid else {}
        client = ScalpClient([rnd], [(now, 100_000.0)],
                             {(1, Side.UP): [(0.50, 10_000)]}, {}, bids=bids)
        c = scalp_cfg(db_path=self.db, live=True, **cfgkw)
        t = build_trader(client, c, self.db)
        self._built.append(t)
        sig = Signal(Side.UP, 0.5, 0.50, 0.0, 10.0, secs_left)
        tid = t._journal.record("PAPER", rnd, sig, 0.0, 0.0, 100.0)
        key = ("BTCUSDT", Side.UP)
        t._positions[key] = Position(tid, rnd, sig, 10.0, 1)
        tp, stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        t._arm_bracket(key, 0.50, tp, stop)
        return t, client, key

    def test_before_the_deadline_nothing_is_touched(self):
        t, client, key = self._at(120.0)
        t._flatten_scalps()
        self.assertEqual(client.cancelled, [])
        self.assertIn(key, t._positions)

    def test_at_the_deadline_the_bracket_is_disarmed(self):
        t, client, key = self._at(60.0)
        t._flatten_scalps()
        self.assertEqual(t._brackets, {})
        self.assertEqual(client.cancelled, [])

    def test_at_the_deadline_the_position_is_offered_back(self):
        t, _, key = self._at(60.0)
        t._flatten_scalps()
        sale = [p for p in t._pending.values()
                if p.plan.action is m.Action.SELL]
        self.assertEqual(len(sale), 1)

    def test_dust_left_by_a_partial_sale_is_not_offered_back(self):
        """
        A partial exit can leave a remnant worth a fraction of a cent. The
        venue refuses a sale that small (SYSTEM_ERROR, -9000), and reporting
        it as a FULL-STAKE bet mislabels a position worth 0.0027 USDT.
        """
        import dataclasses as _dc
        t, _, key = self._at(60.0)
        t._positions[key] = _dc.replace(t._positions[key],
                                        committed_usdt=0.0027)
        with self.assertNoLogs("btc5m", level="ERROR"):
            t._flatten_scalps()
        sale = [p for p in t._pending.values()
                if p.plan.action is m.Action.SELL]
        self.assertEqual(sale, [])

    def test_a_round_is_flattened_once(self):
        """
        Re-running would cancel the very sale the first pass placed, every
        pass, forever.
        """
        t, client, key = self._at(60.0)
        t._flatten_scalps()
        before = list(client.cancelled)
        t._flatten_scalps()
        self.assertEqual(client.cancelled, before)

    def test_a_failed_flatten_leaves_the_position_to_settle(self):
        """
        The honest fallback: retrying into a book that is not there is how a
        5% loss becomes a 100% one.
        """
        t, _, key = self._at(60.0, bid=None)
        with self.assertLogs("btc5m", level="ERROR") as logged:
            t._flatten_scalps()
        self.assertIn(key, t._positions)
        self.assertTrue(any("FULL-STAKE" in line for line in logged.output))

    def test_nothing_happens_when_the_profile_is_off(self):
        t, client, _ = self._at(60.0)
        t._static_cfg = cfg(db_path=self.db)
        t._flatten_scalps()
        self.assertEqual(client.cancelled, [])
