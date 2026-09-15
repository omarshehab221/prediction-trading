"""
Shadow mode: the live code path against the real venue, with no order sent.

Paper mode is a different program from live -- a notional bankroll, screen
prices instead of quotes, no fill records -- so it could not show what the
live bot would do. Shadow mode runs the live path itself: the real balance,
the real books, the venue's own (non-binding) quotes. Only the three writes
are simulated: placing an order, cancelling one, and redeeming.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import btc_5m_predictor as m
from btc5m.cli import mode_from_env
from btc5m.domain import Action, OrderPlan, OrderType, Side
from btc5m.venue.client import PredictionClient
from btc5m.venue.shadow import ShadowClient, ShadowWriteBlocked
from tests.support import _close_journals, cfg, make_round


def _quote(avg, out, plan):
    return m.Quote(f"q-{plan.side.value}-{plan.action.value}", avg, out, 0.0,
                   0.0, action=plan.action, order_type=plan.order_type,
                   price_limit=plan.price_limit)


class TestShadowClient(unittest.TestCase):

    def setUp(self):
        self.rnd = make_round()
        self.c = ShadowClient(cfg(live=True, shadow=True))
        for patch in (
                mock.patch.object(PredictionClient, "balance_usdt",
                                  return_value=7.46),
                mock.patch.object(PredictionClient, "get_quote",
                                  side_effect=lambda rnd, plan:
                                  _quote(0.50, 1.967, plan)),
                mock.patch.object(PredictionClient, "bids_for",
                                  return_value=[(0.48, 100.0)])):
            patch.start()
            self.addCleanup(patch.stop)

    def _buy(self, stake=1.0):
        plan = m._market_buy(Side.UP, stake)
        return self.c.place_order(self.rnd, self.c.get_quote(self.rnd, plan),
                                  stake)

    @staticmethod
    def _sell_plan(shares, limit):
        return OrderPlan(side=Side.UP, action=Action.SELL,
                         order_type=OrderType.LIMIT, amount=shares,
                         price_limit=limit)

    def test_no_write_ever_reaches_the_network(self):
        with mock.patch.object(PredictionClient, "_request") as sent:
            for name in ("place_order", "batch_cancel", "batch_redeem"):
                with self.assertRaises(ShadowWriteBlocked):
                    self.c._request(name, {})
        sent.assert_not_called()

    def test_a_market_buy_fills_at_the_quote_and_debits_the_balance(self):
        with mock.patch.object(PredictionClient, "_request") as sent:
            oid = self._buy()
        sent.assert_not_called()
        self.assertTrue(oid.startswith("shadow-"))
        self.assertAlmostEqual(self.c.confirm_fill(oid, 1.0), 1.0)
        # The venue holds shares to two decimals, rounded down here.
        self.assertAlmostEqual(self.c.delivered_shares(oid), 1.96)
        self.assertAlmostEqual(self.c.executed_price(oid), 0.50)
        self.assertAlmostEqual(self.c.balance_usdt(), 6.46)

    def test_selling_more_than_is_held_is_refused_like_the_venue(self):
        self._buy()                                   # holds 1.96
        with self.assertRaises(m.ApiError) as ctx:
            self.c.get_quote(self.rnd, self._sell_plan(2.0, 0.45))
        self.assertEqual(ctx.exception.code, -9000)

    def test_a_sell_quote_is_priced_at_its_limit_without_the_venue(self):
        """
        The real account holds none of the shadow's shares, so the venue
        would refuse every sell quote with -9000 and no stop would ever sell.
        The quote is built here, priced at the limit as the venue prices a
        limit sell's amountOut.
        """
        self._buy()
        with mock.patch.object(PredictionClient, "get_quote") as venue:
            q = self.c.get_quote(self.rnd, self._sell_plan(1.95, 0.45))
        venue.assert_not_called()
        self.assertAlmostEqual(q.amount_out, 1.95 * 0.45)

    def test_a_limit_sell_fills_against_the_live_bid_net_of_the_fee(self):
        self._buy()
        plan = self._sell_plan(1.95, 0.45)
        oid = self.c.place_order(self.rnd, self.c.get_quote(self.rnd, plan))
        state = self.c.order_state(oid)
        self.assertEqual(state.status, "FILLED")
        self.assertAlmostEqual(state.filled_shares, 1.95)
        net = 1.95 * 0.48 * (1 - self.rnd.fee_bps / 10_000)
        self.assertAlmostEqual(state.filled_usdt, net)
        self.assertAlmostEqual(self.c.balance_usdt(), 7.46 - 1.0 + net)

    def test_a_limit_sell_with_no_bid_at_its_limit_rests_until_cancelled(self):
        self._buy()
        plan = self._sell_plan(1.95, 0.49)            # the bid is 0.48
        oid = self.c.place_order(self.rnd, self.c.get_quote(self.rnd, plan))
        self.assertEqual(self.c.order_state(oid).status, "RESTING")
        self.assertEqual(self.c.cancel_orders([oid]), ([oid], {}))
        self.assertEqual(self.c.order_state(oid).status, "DEAD")

    def test_a_redeem_credits_the_winning_shares_and_sends_nothing(self):
        self._buy()
        token = self.rnd.token_for(Side.UP)
        with mock.patch.object(PredictionClient, "_request") as sent:
            hashes = self.c.batch_redeem([token], self.rnd.chain_id)
            statuses = [self.c.redeem_status(h) for h in hashes]
        sent.assert_not_called()
        self.assertEqual(statuses, ["SUCCESS"])
        self.assertAlmostEqual(self.c.balance_usdt(), 7.46 - 1.0 + 1.96)


class TestShadowMode(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def test_a_shadow_config_trades_through_the_shadow_client(self):
        t = m.Trader(cfg(db_path=self.db, live=True, shadow=True))
        self.addCleanup(t._journal.close)
        self.assertIsInstance(t._client, ShadowClient)
        self.assertEqual(t._mode_label, "SHADOW")

    def test_a_live_config_still_uses_the_real_client(self):
        t = m.Trader(cfg(db_path=self.db, live=True))
        self.addCleanup(t._journal.close)
        self.assertNotIsInstance(t._client, ShadowClient)
        self.assertEqual(t._mode_label, "LIVE")


class TestTradingModeFromEnvironment(unittest.TestCase):

    def test_shadow_pins_live_data_through_the_shadow_client(self):
        self.assertEqual(mode_from_env(None, "shadow"), (True, True))

    def test_live_paper_and_unset_are_unchanged(self):
        self.assertEqual(mode_from_env(None, "live"), (True, False))
        self.assertEqual(mode_from_env(None, "paper"), (False, False))
        self.assertEqual(mode_from_env(None, ""), (None, False))
        self.assertEqual(mode_from_env(True, ""), (True, False))

    def test_a_flag_conflicting_with_shadow_is_refused(self):
        # --live beside TRADING_MODE=shadow must never quietly trade for real.
        for flag in (True, False):
            with self.assertRaises(ValueError):
                mode_from_env(flag, "shadow")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            mode_from_env(None, "shaddow")


if __name__ == "__main__":
    unittest.main()
