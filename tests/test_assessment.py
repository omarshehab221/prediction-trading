"""The gates between a round and a position."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config,
    PredictionClient,
    Side,
    assess,
    breakeven_probability,
    digital_up_probability,
)
from tests.support import cfg, make_round
from tests.test_venue import TestParseRound

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


class TestGatesSeeThePricePaid(unittest.TestCase):
    """Every gate must be checked against the price that will actually fill."""

    def _assess(self, ask, precision, min_edge=0.02):
        c = cfg(min_edge=min_edge, min_edge_ratio=0.0, min_entry_price=0.05,
                max_entry_price=0.95, max_blended_price=0.90,
                min_win_return=0.0, entry_window_start_s=300,
                entry_window_end_s=1)
        rnd = make_round(decimal_precision=precision, fee_bps=0)
        spot = rnd.strike * (1 - 0.0006)
        verdict = m.assess(rnd, spot, 0.5, 100.0, rnd.end_ms - 60_000, c,
                           {Side.UP: [(ask, 10_000.0)],
                            Side.DOWN: [(ask, 10_000.0)]},
                           None, m.Trend())
        return c, rnd, verdict

    def test_the_recorded_edge_clears_the_floor_it_was_gated_on(self):
        """
        Regression: the edge floor was checked BEFORE the price was snapped
        to the market's precision, and the edge was recorded AFTER. Rounding
        up raises breakeven, so a trade could be taken and journalled with a
        real edge below min_edge -- the floor having been tested against a
        price that was never going to be the fill.

        These numbers are a real instance: at precision 1 an ask of 0.753
        clears a 0.02 floor, fills at 0.80, and leaves an edge of 0.0079.
        """
        c, rnd, verdict = self._assess(0.753, precision=1)
        if verdict.signal is None:
            # The correct answer: at 0.80 the edge does not clear, so the
            # round is refused rather than taken on a price nobody will pay.
            self.assertEqual(verdict.blocked_by, "edge below the floor")
            return
        self.assertGreaterEqual(
            verdict.signal.edge, c.min_edge - 1e-12,
            "a signal was recorded with an edge under the floor that was "
            "supposed to gate it")
        self.assertTrue(
            m.clears_edge(verdict.signal.model_prob,
                          verdict.signal.fill_price, c, rnd.fee_bps),
            "the recorded fill price does not clear the edge gate")

    def test_a_coarse_market_can_still_trade_when_the_edge_survives(self):
        """The fix must decline the bad case, not every case at precision 1."""
        c, rnd, verdict = self._assess(0.753, precision=1, min_edge=0.001)
        self.assertIsNotNone(verdict.signal)
        self.assertEqual(verdict.signal.fill_price,
                         round(verdict.signal.fill_price, 1))
        self.assertGreaterEqual(verdict.signal.edge, c.min_edge - 1e-12)


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


class TestOnlyBufferScalesIn(unittest.TestCase):
    """
    The sizing change was requested for buffer; it must not leak.

    hybrid is the one sanctioned carrier: its buffer layer IS buffer, gates
    and scale-in alike, so it is held to buffer's exact values below rather
    than exempted from the rule.
    """

    SCALES_IN = {"buffer", "hybrid"}
    SCALE_IN_KEYS = ("scale_in", "scale_in_initial_pct", "scale_in_min_topup",
                     "max_blended_price")

    def test_scale_in_is_enabled_only_for_buffer(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertEqual(c.scale_in, name in self.SCALES_IN, name)

    def test_hybrid_scales_in_exactly_as_buffer_does(self):
        buf = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        hyb = Config(api_key="k", api_secret="s", **m.PROFILES["hybrid"])
        for key in self.SCALE_IN_KEYS:
            self.assertEqual(getattr(hyb, key), getattr(buf, key), key)

    def test_only_buffer_opens_below_full_kelly(self):
        for name, prof in m.PROFILES.items():
            c = Config(api_key="k", api_secret="s", **prof)
            mid = (c.min_entry_price + c.max_entry_price) / 2
            full = m.kelly_stake(100.0, min(mid * 1.5, 0.99), mid, c, 200)
            if name in self.SCALES_IN:
                self.assertLess(c.scale_in_initial_pct, 1.0)
            else:
                opener = full
                self.assertAlmostEqual(opener, full, places=9, msg=name)

    def test_other_profiles_keep_the_standard_opener_fraction(self):
        for name, prof in m.PROFILES.items():
            if name in self.SCALES_IN:
                continue
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertAlmostEqual(c.scale_in_initial_pct, 0.4, msg=name)

    def test_buffer_opens_smaller_than_the_others(self):
        buf = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        other = Config(api_key="k", api_secret="s", **m.PROFILES["micro"])
        self.assertLess(buf.scale_in_initial_pct, other.scale_in_initial_pct)
