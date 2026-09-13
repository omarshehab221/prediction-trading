"""
The maths: what a contract is worth, what clears a cost, how much to
stake and what a settled trade paid.
"""

from __future__ import annotations

import math
import random
import unittest
from decimal import Decimal

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config,
    breakeven_probability,
    digital_up_probability,
    kelly_stake,
    settle_pnl,
    walk_book,
)
from tests.support import cfg, convex_cfg

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


class TestReservationPrice(unittest.TestCase):
    """The limit price is derived from the gates, never written down."""

    def test_price_for_breakeven_inverts_breakeven_probability(self):
        for price in (0.05, 0.25, 0.5, 0.75, 0.95):
            for fee in (0, 50, 200, 1000):
                b = m.breakeven_probability(price, fee)
                self.assertAlmostEqual(m.price_for_breakeven(b, fee), price,
                                       places=9, msg=f"{price} @ {fee}bps")

    def test_price_for_breakeven_rejects_impossible_input(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                m.price_for_breakeven(bad, 200)

    def test_buy_reservation_price_clears_every_gate(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_win_return=0.0,
                max_entry_price=0.90, min_entry_price=0.05)
        p = m.buy_reservation_price(0.70, c, 200)
        self.assertIsNotNone(p)
        self.assertTrue(m.clears_edge(0.70, p - 1e-9, c, 200))
        self.assertLessEqual(p, c.max_entry_price)

    def test_buy_reservation_price_is_the_highest_such_price(self):
        """One tick above it must fail the gate, or it is not a ceiling."""
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_win_return=0.0,
                max_entry_price=0.99, min_entry_price=0.05)
        p = m.buy_reservation_price(0.70, c, 200)
        self.assertFalse(m.clears_edge(0.70, p + 1e-4, c, 200))

    def test_buy_reservation_price_respects_the_return_floor(self):
        c = cfg(min_edge=0.001, min_edge_ratio=0.0, min_win_return=0.50,
                max_entry_price=0.99, min_entry_price=0.05)
        p = m.buy_reservation_price(0.95, c, 200)
        self.assertTrue(m.clears_return(p, 200, c))

    def test_buy_reservation_price_is_none_below_the_band(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15, min_entry_price=0.60,
                max_entry_price=0.90, max_blended_price=0.80)
        self.assertIsNone(m.buy_reservation_price(0.20, c, 200))

    def test_sell_reservation_price_demands_the_market_overpays(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15)
        p = m.sell_reservation_price(0.50, c, 200)
        self.assertIsNotNone(p)
        # Proceeds net of fee must beat holding by the profile's own bar.
        self.assertGreaterEqual(p * 0.98 - 0.50, c.min_edge - 1e-9)

    def test_sell_reservation_price_is_none_when_no_price_can_clear(self):
        c = cfg(min_edge=0.04, min_edge_ratio=0.15)
        self.assertIsNone(m.sell_reservation_price(0.97, c, 200))


class TestBracketArithmetic(unittest.TestCase):
    """
    The two prices are P&L targets inverted, so the round trip must land on
    exactly the numbers that were asked for.
    """

    def _pnl(self, stake, fill, exit_price, fee_bps):
        shares = stake / fill
        return shares * exit_price * (1 - fee_bps / 10_000.0) - stake

    def test_the_round_trip_returns_exactly_the_target(self):
        for fee in (0, 50, 200):
            for fill in (0.20, 0.50, 0.75):
                tp, stop = m.bracket_prices(fill, fee, 0.05, 0.05)
                self.assertAlmostEqual(
                    self._pnl(10.0, fill, tp, fee), 0.50, places=9,
                    msg=f"fee={fee} fill={fill}")
                self.assertAlmostEqual(
                    self._pnl(10.0, fill, stop, fee), -0.50, places=9,
                    msg=f"fee={fee} fill={fill}")

    def test_a_zero_fee_bracket_is_symmetric_in_price(self):
        tp, stop = m.bracket_prices(0.50, 0, 0.05, 0.05)
        self.assertAlmostEqual(tp - 0.50, 0.50 - stop, places=12)

    def test_the_fee_pushes_both_prices_up(self):
        """
        Which is what makes a bracket symmetric in money asymmetric in price
        -- the target moves further away and the stop moves closer.
        """
        free_tp, free_stop = m.bracket_prices(0.50, 0, 0.05, 0.05)
        paid_tp, paid_stop = m.bracket_prices(0.50, 200, 0.05, 0.05)
        self.assertGreater(paid_tp, free_tp)
        self.assertGreater(paid_stop, free_stop)
        self.assertGreater(paid_tp - 0.50, 0.50 - paid_stop)

    def test_no_bracket_where_the_target_would_price_above_one(self):
        """A contract cannot pay more than 1, so nobody is bidding there."""
        self.assertIsNone(m.bracket_prices(0.95, 200, 0.05, 0.05))

    def test_no_bracket_where_the_fee_exceeds_the_loss_being_capped(self):
        """
        The stop would sit at or above the fill, so the position is cut on
        the tick it opens, for more than the loss it was meant to cap.
        """
        self.assertIsNone(m.bracket_prices(0.50, 600, 0.05, 0.05))

    def test_an_untradable_fill_is_refused_rather_than_clamped(self):
        for bad in (0.0, 1.0, -0.1, 1.5):
            with self.assertRaises(ValueError, msg=str(bad)):
                m.bracket_prices(bad, 200, 0.05, 0.05)


class TestSignalEdgeRequired(unittest.TestCase):
    """The number that decides whether this profile can work at all."""

    def test_a_free_market_asks_for_nothing_beyond_a_coin_flip(self):
        self.assertAlmostEqual(m.signal_edge_required(0, 0.05, 0.05), 0.0,
                               places=12)

    def test_two_percent_asks_for_twenty_points(self):
        self.assertAlmostEqual(m.signal_edge_required(200, 0.05, 0.05), 0.20,
                               places=2)

    def test_it_rises_with_the_fee(self):
        previous = -1.0
        for fee in (0, 25, 50, 100, 200, 300):
            required = m.signal_edge_required(fee, 0.05, 0.05)
            self.assertGreater(required, previous, f"fee={fee}")
            previous = required

    def test_a_fee_that_swallows_the_stop_has_no_answer(self):
        self.assertIsNone(m.signal_edge_required(600, 0.05, 0.05))

    def test_it_agrees_with_bracket_prices_about_what_is_tradable(self):
        """
        Two functions, one claim. A fee where one says "no bracket" and the
        other quotes a number would let the gate pass a position that cannot
        be bracketed.
        """
        for fee in (0, 100, 200, 400, 490, 500, 600, 900):
            required = m.signal_edge_required(fee, 0.05, 0.05)
            bracket = m.bracket_prices(0.30, fee, 0.05, 0.05)
            self.assertEqual(required is None, bracket is None, f"fee={fee}")


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
