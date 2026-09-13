"""What the bot measures about the market and what makes it stop."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace

import btc_5m_predictor as m
from btc_5m_predictor import Config, RiskManager, Side, TradingHalted
from tests.support import cfg, convex_cfg

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

    def test_nothing_is_left_over_for_the_next_call_to_find(self):
        """
        The leak. Drift is measured against the baseline ABSOLUTELY, not
        since the last call, so anything left unabsorbed is measured again
        next time and every time after. Sub-threshold drift therefore did not
        stay small -- it stayed INVISIBLE while it accumulated, then crossed
        the threshold in one step and was rebased away as somebody's deposit.
        After reconciling, the decomposition has to add back up to the number
        the venue reported.
        """
        r = RiskManager(cfg(), 100.0)
        r.record_result(False, 0.5, pnl=-5.0)
        r.check(94.97, 0.0)               # 0.03 unexplained, under tolerance

        self.assertAlmostEqual(r._day_start_bankroll + r.realised_pnl,
                               94.97, places=9)

    def test_a_steady_small_leak_does_not_accumulate_unseen(self):
        """
        Three cents a round, ten rounds. Under the old threshold none of the
        first few were absorbed, the total crossed 0.10, and the whole
        accumulation was announced as an external movement -- a laundering
        caused by the very threshold meant to be too small to matter.
        """
        # Neither the streak counter nor the daily limit is what is under
        # test here; both are lifted out of the way.
        r = RiskManager(cfg(max_consecutive_losses=99,
                            daily_loss_limit_pct=0.90), 100.0)
        balance = 100.0
        for _ in range(10):
            r.record_result(False, 0.5, pnl=-1.0)
            balance -= 1.03               # the venue takes 0.03 more each time
            r.check(balance, 0.0)
            self.assertAlmostEqual(r._day_start_bankroll + r.realised_pnl,
                                   balance, places=9)

        self.assertAlmostEqual(balance, 89.70, places=9)
        self.assertAlmostEqual(r.external_flow, -0.30, places=6)

    def test_a_corrected_loss_still_trips_the_daily_limit(self):
        """
        The point of all of it. A model that under-reports every loss must
        not be able to walk the bot past its own stop. The correction comes
        from the venue's own balance, via a declared credit that landed light.
        """
        r = RiskManager(cfg(daily_loss_limit_pct=0.20), 100.0)
        r.record_result(False, 0.6, pnl=-19.0)
        r.expect_credit(20.0)             # room for a 2.00 fee
        with self.assertRaises(TradingHalted):
            r.check(79.0, 0.0)            # venue took 2.00 more than modelled
        self.assertAlmostEqual(r.pnl_correction, -2.0, places=6)

    def test_the_halt_message_reports_corrections_separately(self):
        r = RiskManager(cfg(daily_loss_limit_pct=0.20), 100.0)
        r.record_result(False, 0.6, pnl=-19.0)
        r.expect_credit(20.0)
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


class TestVolatilityCache(unittest.TestCase):
    """Sigma must be cached per symbol, not globally."""

    def test_separate_symbols_do_not_share_a_cache(self):
        seen = []

        class Feed:
            def closes(self, symbol):
                seen.append(symbol)
                # Distinct, small-amplitude series so neither hits the
                # volatility ceiling and gets clamped to the same value.
                amp = 0.0005 if symbol == "BTCUSDT" else 0.0020
                return [100.0 * (1 + amp * (i % 2)) for i in range(30)]

        v = m.VolatilityEstimator(cfg(), Feed())
        a = v.sigma_annual("BTCUSDT")
        b = v.sigma_annual("BTCUSD")
        self.assertEqual(seen, ["BTCUSDT", "BTCUSD"])
        self.assertNotEqual(a, b)
        v.sigma_annual("BTCUSDT")
        self.assertEqual(len(seen), 2)      # cached, not refetched


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


class TestClampedSigmaGuard(unittest.TestCase):
    """A clamped sigma is an assertion, not a measurement."""

    def test_malformed_closes_do_not_crash(self):
        rows = [float(v) for v in
                ["100", "0", "101", "-5", "102", "103"] * 40]

        class Feed:
            def closes(self, symbol):
                return list(rows)
        est = m.VolatilityEstimator(cfg(), Feed())
        self.assertGreater(est.sigma_annual(), 0.0)   # survives, no exception

    def _est(self, per_min_sd):
        import random
        rng = random.Random(2)
        rows = []
        px = 100.0
        for _ in range(500):
            px *= math.exp(rng.gauss(0, per_min_sd))   # stays positive
            rows.append(px)

        class Feed:
            def closes(self, symbol):
                return list(rows)
        return m.VolatilityEstimator(cfg(), Feed())

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


class TestRawSigmaDiagnostic(unittest.TestCase):

    def _est(self, sd):
        import random
        rng = random.Random(2); rows = []; px = 100.0
        for _ in range(500):
            px *= math.exp(rng.gauss(0, sd))
            rows.append(px)

        class Feed:
            def closes(self, symbol):
                return list(rows)
        return m.VolatilityEstimator(cfg(), Feed())

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


class TestVolatilityReadsMarketData(unittest.TestCase):
    """The estimator must take its closes from the seam, not its own fetch."""

    def test_sigma_uses_the_closes_market_data_supplies(self):
        closes = [100.0 * (1.0 + 0.0001 * i) for i in range(120)]

        class FakeMarketData:
            calls = 0

            def closes(self, symbol):
                type(self).calls += 1
                return list(closes)

        md = FakeMarketData()
        est = m.VolatilityEstimator(replace(cfg(), vol_lookback_min=120,
                                            sigma_window_min=60), md)
        sigma = est.sigma_annual("BTCUSDT")
        self.assertGreater(sigma, 0.0)
        self.assertEqual(FakeMarketData.calls, 1)

    def test_too_few_closes_is_an_api_error_not_a_crash(self):
        class ThinMarketData:
            def closes(self, symbol):
                return [100.0, 100.1]

        est = m.VolatilityEstimator(cfg(), ThinMarketData())
        with self.assertRaises(m.ApiError):
            est.sigma_annual("BTCUSDT")
