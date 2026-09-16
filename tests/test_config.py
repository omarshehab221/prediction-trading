"""
Settings: what they mean, which profile sets them, and what happens
when the file on disk changes underneath a running bot.
"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest
from dataclasses import replace

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config,
    PredictionClient,
    Side,
    Trader,
    TradingHalted,
    assess,
    settle_pnl,
)
from tests.support import (
    ROOT,
    _close_journals,
    cfg,
    convex_cfg,
    hybrid_cfg,
    lastminute_cfg,
    make_round,
    scalp_cfg,
    straddle_cfg,
)
from tests.test_venue import TestParseRound

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

    def test_default_profile_is_the_scalp_one(self):
        self.assertEqual(m.DEFAULT_PROFILE, "scalp")

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


class TestConfigFile(unittest.TestCase):
    """Every setting lives in the file, and the file round-trips."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json"); os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        _close_journals(self)
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
        _close_journals(self)
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


class TestLimitConfig(unittest.TestCase):
    """Three enums, and the one pairing that describes an impossible order."""

    def test_defaults_are_todays_behaviour(self):
        c = cfg()
        self.assertEqual(c.entry_order_type, "MARKET")
        self.assertEqual(c.exit_order_type, "NONE")

    def test_every_existing_profile_still_sends_market_and_never_exits(self):
        """
        `scalp` is the one profile that leaves before settlement, and it
        leaves through BRACKET rather than through the model-priced exit --
        so it is excluded by name here and covered by TestScalpProfile.
        """
        for name, prof in m.PROFILES.items():
            if name == "scalp":
                continue
            c = Config(api_key="k", api_secret="s", **prof)
            self.assertEqual(c.entry_order_type, "MARKET", name)
            self.assertEqual(c.exit_order_type, "NONE", name)

    def test_unknown_order_types_are_rejected(self):
        for field, bad in (("entry_order_type", "STOP"),
                           ("exit_order_type", "MAYBE"),
                           ("exit_trigger", "SOMETIMES")):
            with self.assertRaises(ValueError, msg=field):
                cfg(**{field: bad})

    def test_a_market_order_cannot_rest(self):
        """
        Coercing this pairing would mean the config says one thing and the
        bot does another, which is worse than refusing to start.
        """
        with self.assertRaises(ValueError):
            cfg(exit_order_type="MARKET", exit_trigger="RESTING")

    def test_a_polled_market_exit_is_allowed(self):
        c = cfg(exit_order_type="MARKET", exit_trigger="POLLED")
        self.assertEqual(c.exit_order_type, "MARKET")

    def test_a_resting_limit_exit_is_allowed(self):
        c = cfg(exit_order_type="LIMIT", exit_trigger="RESTING")
        self.assertEqual(c.exit_trigger, "RESTING")

    def test_the_generated_config_document_carries_the_new_fields(self):
        doc = m.default_config_document()
        for field in ("entry_order_type", "exit_order_type", "exit_trigger"):
            self.assertIn(field, doc["defaults"], field)


class TestScalpConfig(unittest.TestCase):

    def test_the_profile_is_internally_valid(self):
        scalp_cfg()                        # must not raise

    def test_the_switch_is_off_everywhere_else(self):
        self.assertFalse(cfg().scalp)

    def test_it_cannot_run_alongside_another_entry_strategy(self):
        for other in ("straddle", "last_minute"):
            with self.assertRaises(ValueError, msg=other):
                scalp_cfg(**{other: True})

    def test_it_cannot_scale_in(self):
        """A top-up moves the fill price the bracket was computed from."""
        with self.assertRaises(ValueError):
            scalp_cfg(scale_in=True)

    def test_bracket_and_scalp_are_one_decision(self):
        """Neither is allowed without the other, in both directions."""
        with self.assertRaises(ValueError):
            scalp_cfg(exit_order_type="LIMIT")
        with self.assertRaises(ValueError):
            cfg(exit_order_type="BRACKET")

    def test_entries_must_stop_before_the_flatten_deadline(self):
        with self.assertRaises(ValueError):
            scalp_cfg(entry_window_end_s=60, scalp_flatten_s=60.0)
        with self.assertRaises(ValueError):
            scalp_cfg(entry_window_end_s=45, scalp_flatten_s=60.0)

    def test_a_tick_must_be_able_to_span_the_window_it_is_measured_over(self):
        with self.assertRaises(ValueError):
            scalp_cfg(scalp_lookback_ms=3000.0, scalp_max_tick_age_ms=1000.0)

    def test_bounds_on_the_new_numbers(self):
        for field, bad in (("scalp_stake_pct", 0.0),
                           ("scalp_stake_pct", 0.3),
                           ("scalp_take_profit_pct", 0.0),
                           ("scalp_take_profit_pct", 1.0),
                           ("scalp_stop_loss_pct", 0.0),
                           ("scalp_stop_loss_pct", 1.0),
                           ("scalp_lookback_ms", 0.0),
                           ("scalp_min_move_bps", 0.0),
                           ("scalp_min_basis_bps", -1.0),
                           ("scalp_max_tick_age_ms", 0.0),
                           ("scalp_cooldown_s", -1.0),
                           ("scalp_max_entries_per_round", 0),
                           ("scalp_flatten_s", -1.0),
                           ("scalp_max_edge_required", 0.5),
                           ("scalp_max_edge_required", -0.1)):
            with self.assertRaises(ValueError, msg=f"{field}={bad}"):
                scalp_cfg(**{field: bad})

    def test_the_generated_config_document_carries_the_new_fields(self):
        doc = m.default_config_document()
        for field in ("scalp", "scalp_stake_pct", "scalp_take_profit_pct",
                      "scalp_stop_loss_pct", "scalp_flatten_s",
                      "scalp_max_edge_required", "ws_futures_url"):
            self.assertIn(field, doc["defaults"], field)
        self.assertIn("scalp", doc["profiles"])


class TestScalpProfile(unittest.TestCase):
    """The futures-lead profile, and the shape it commits to."""

    def _c(self):
        return Config(api_key="k", api_secret="s", **m.PROFILES["scalp"])

    def test_it_takes_on_entry_and_rests_on_exit(self):
        """
        The premise is being early to a move the book has not priced, which
        a resting bid cannot be: it fills when someone wants to sell into it.
        """
        c = self._c()
        self.assertTrue(c.scalp)
        self.assertEqual(c.entry_order_type, "MARKET")
        self.assertEqual(c.exit_order_type, "BRACKET")

    def test_entries_stop_before_the_flatten_not_on_it(self):
        """A scalp opened at the deadline would be closed the same instant."""
        c = self._c()
        self.assertGreater(c.entry_window_end_s, c.scalp_flatten_s)

    def test_nothing_is_placed_inside_the_last_minute(self):
        """The whole reason the profile stops early."""
        c = self._c()
        self.assertLessEqual(c.scalp_flatten_s, 60.0)

    def test_the_bracket_is_the_asymmetric_pair_that_was_asked_for(self):
        """
        12 up, 5 down. Both legs sell through the bid, but the stop fires
        into a falling book and overshoots: live, wins landed on their 5%
        target while losses averaged 11.7% against the same 5%.
        """
        c = self._c()
        self.assertAlmostEqual(c.scalp_take_profit_pct, 0.12)
        self.assertAlmostEqual(c.scalp_stop_loss_pct, 0.05)
        self.assertGreater(c.scalp_take_profit_pct, c.scalp_stop_loss_pct)
        # The stake is 10% of bankroll; the pair above is the bracket.
        self.assertAlmostEqual(c.scalp_stake_pct, 0.10)

    def test_the_stake_cap_and_the_scalp_stake_agree(self):
        """
        Two names for one number. Letting them drift would mean the shared
        risk checks reason about a size this profile never commits.
        """
        c = self._c()
        self.assertAlmostEqual(c.scalp_stake_pct, c.max_stake_pct)

    def test_the_round_trip_ceiling_survives_a_full_day(self):
        """
        rounds_today counts round TRIPS here, so a per-round ceiling of 20
        at twelve rounds an hour must not halt the bot before lunch.
        """
        c = self._c()
        per_hour = c.scalp_max_entries_per_round * 12
        self.assertGreaterEqual(c.max_rounds_per_day, per_hour * 8)

    def test_it_survives_the_shared_profile_checks(self):
        prof = m.PROFILES["scalp"]
        for required in ("paper_start_bankroll", "daily_loss_limit_pct",
                         "assumed_spread_pct"):
            self.assertIn(required, prof, required)
        c = self._c()
        self.assertGreaterEqual(c.daily_loss_limit_pct / c.max_stake_pct, 2.5)

    def test_the_band_leaves_room_for_a_take_profit_to_exist(self):
        """
        At the top of the book a 5% gain prices above 1.00 and no bracket
        fits, so the band has to stop below wherever that happens.
        """
        c = self._c()
        self.assertIsNotNone(
            m.bracket_prices(c.max_entry_price, 0, c.scalp_take_profit_pct,
                             c.scalp_stop_loss_pct))

    def test_no_other_profile_scalps(self):
        for name, prof in m.PROFILES.items():
            if name == "scalp":
                continue
            self.assertFalse(prof.get("scalp", False), name)


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

    def test_the_straddle_profile_does_not_force_a_hedge(self):
        """
        An unhedged leg rides to settlement rather than being closed out at
        the deadline. At the 0.40 this profile now opens at, the other side
        near the deadline costs about 0.60 -- paying most of the pair to turn
        a near-even bet into a certain loss. Force hedging was written for a
        0.25 opener against a 0.75 hedge, which is no longer the usual case.
        """
        self.assertFalse(straddle_cfg().straddle_force_hedge)

    def test_forcing_a_hedge_is_still_the_default_off_this_profile(self):
        """The profile overrides it; the reasoning behind the default stands."""
        self.assertTrue(m.Config.straddle_force_hedge)

    def test_the_straddle_profile_gates_on_the_payout_test(self):
        """
        The gate is the profile. Without it the bot buys both sides of any
        round at any price, which is a coin flip paying a fee, not a hedge.
        """
        c = straddle_cfg()
        self.assertTrue(c.straddle_require_positive_worst_case)
        self.assertEqual(c.straddle_min_worst_case_return, 0.0)


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


class TestWsConfig(unittest.TestCase):
    """The WebSocket settings, and the invariants that keep them honest."""

    def test_defaults_are_present_and_sane(self):
        c = cfg()
        self.assertTrue(c.ws_enabled)
        self.assertTrue(c.ws_spot_url.startswith("wss://"))
        self.assertTrue(c.ws_book_url.startswith("wss://"))
        self.assertGreater(c.ws_stale_s, 0)
        self.assertGreater(c.ws_reconnect_max_s, 0)
        self.assertGreater(c.ws_recycle_s, 0)

    def test_recycle_lands_before_the_venue_closes_the_socket(self):
        """
        The venue drops the connection at 24h. Recycling at or after that
        means the drop always arrives as a surprise disconnect rather than a
        planned handover, which is a gap in the feed on a fixed schedule.
        """
        self.assertLess(cfg().ws_recycle_s, 24 * 3600)

    def test_a_non_positive_stale_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_stale_s=0.0)

    def test_a_non_positive_reconnect_ceiling_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_reconnect_max_s=-1.0)

    def test_a_recycle_at_or_past_the_venue_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(cfg(), ws_recycle_s=24 * 3600)

    def test_ws_url_resolves_both_feeds(self):
        c = cfg()
        self.assertEqual(c.ws_url("spot"), c.ws_spot_url)
        self.assertEqual(c.ws_url("book"), c.ws_book_url)

    def test_ws_url_refuses_a_name_it_does_not_know(self):
        with self.assertRaises(KeyError):
            cfg().ws_url("orderbook")

    def test_the_validation_tolerance_is_tighter_than_a_transposed_side(self):
        """
        A transposed side mapping shows up as |1 - 2p| of price error, which
        is 0.20 at p=0.40 and grows toward the ends. The tolerance has to sit
        well below that or the check it exists to perform cannot fail.
        """
        self.assertLess(m.WS_BOOK_VALIDATE_TOL, 0.10)
        self.assertGreater(m.WS_BOOK_VALIDATE_TOL, 0.0)

    def test_websocket_client_is_declared(self):
        import os as _os
        here = ROOT
        with open(_os.path.join(here, "requirements.txt")) as fh:
            self.assertIn("websocket-client", fh.read())

    def test_the_image_ships_the_transport_module(self):
        import os as _os
        here = ROOT
        path = _os.path.join(here, "Dockerfile")
        if not _os.path.exists(path):
            self.skipTest("Dockerfile not present")
        with open(path) as fh:
            text = fh.read()
        self.assertIn("ws_feeds.py", text,
                      "ws_feeds.py must be COPYed or the image runs without "
                      "the transport layer and silently falls back to REST")


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


class TestHybridConfig(unittest.TestCase):

    def test_the_profile_builds(self):
        c = hybrid_cfg()
        self.assertTrue(c.hybrid)
        self.assertFalse(c.straddle or c.scalp or c.last_minute)

    def test_the_profile_carries_the_agreed_numbers(self):
        c = hybrid_cfg()
        self.assertEqual(c.hybrid_stop_loss_pct, 0.25)
        self.assertEqual(c.hybrid_stop_disarm_s, 45.0)
        self.assertEqual(c.straddle_stake_pct, 0.20)
        self.assertEqual(c.max_stake_pct, 0.10)
        self.assertEqual(c.min_stake_usdt, 1.0)
        self.assertEqual(c.entry_window_start_s, 240)
        self.assertEqual(c.straddle_entry_window_s, 60.0)
        self.assertEqual(c.daily_loss_limit_pct, 0.50)
        self.assertEqual(c.reserve_pct, 0.10)
        self.assertEqual(c.max_concurrent_positions, 4)
        self.assertTrue(c.straddle_require_positive_worst_case)
        self.assertFalse(c.straddle_force_hedge)
        self.assertTrue(c.scale_in)
        self.assertTrue(c.trend_follow)

    def test_hybrid_excludes_every_other_entry_strategy(self):
        for flag in ("straddle", "scalp", "last_minute"):
            with self.assertRaises(ValueError, msg=flag):
                hybrid_cfg(**{flag: True})

    def test_hybrid_refuses_any_exit_but_none(self):
        for exit_type in ("MARKET", "LIMIT", "BRACKET"):
            with self.assertRaises(ValueError, msg=exit_type):
                hybrid_cfg(exit_order_type=exit_type)

    def test_hybrid_refuses_limit_entries(self):
        with self.assertRaises(ValueError):
            hybrid_cfg(entry_order_type="LIMIT")

    def test_stop_loss_pct_must_be_in_zero_one(self):
        for bad in (0.0, 1.0, -0.1):
            with self.assertRaises(ValueError, msg=bad):
                hybrid_cfg(hybrid_stop_loss_pct=bad)

    def test_disarm_window_must_be_non_negative(self):
        with self.assertRaises(ValueError):
            hybrid_cfg(hybrid_stop_disarm_s=-1.0)

    def test_hybrid_is_off_everywhere_else(self):
        for name, values in m.PROFILES.items():
            if name != "hybrid":
                self.assertNotIn("hybrid", values, name)
