"""
Buying when the venue quotes materially less than the model's
probability.
"""

from __future__ import annotations

from dataclasses import replace

import requests

from btc5m.assessment import (
    assess,
    clears_edge,
    clears_return,
    entry_window_start_s,
)
from btc5m.constants import EPS, LOG
from btc5m.domain import Position, Side, _market_buy
from btc5m.errors import ApiError, TradingHalted
from btc5m.pricing import breakeven_probability, win_return
from btc5m.sizing import kelly_multiple


class ModelEntryMixin:
    """The model-edge strategy."""
    def _maybe_enter_model(self, bankroll: float, mode: str) -> None:
        # The cap the config actually declares. An earlier version returned
        # whenever ANY position was open, which made
        # max_concurrent_positions dead: its default of 2 could never be
        # reached, every multi-market deployment silently traded one market
        # at a time, and the setting read as configuration while behaving as
        # a constant.
        #
        # What that guard was really protecting is narrower and is enforced
        # per market below: never open a SECOND position on a symbol that
        # already has one. That is the same bet twice, not diversification,
        # and it would orphan the first in the journal.
        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        now_ms = self._client.now_ms()
        self._prune(now_ms)

        available = self._available(bankroll)
        if available < self._cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a new position "
                      "(%.2f committed of %.2f)", self._committed(), bankroll)
            return

        for raw in self._list_rounds():
            if raw.topic_id in self._seen:
                continue
            # One position per market: a second on the same symbol would be
            # the same bet twice, not diversification.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            # The first straddle_entry_window_s of a round are the straddle
            # layer's. A trend widens buffer's own window past 240s, so the
            # window setting alone cannot keep the two apart.
            if (self._cfg.hybrid and (now_ms - raw.start_ms) / 1000.0
                    <= self._cfg.straddle_entry_window_s):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue
            # The widest window any trend could open. The exact one depends on
            # the trend for THIS market's settlement feed, which is not known
            # until the round is hydrated -- so screen loosely here and let
            # evaluate() apply the real bound. Screening tightly would discard
            # precisely the early rounds the trend exists to catch.
            if not (self._cfg.entry_window_end_s
                    <= raw.seconds_remaining(now_ms)
                    <= entry_window_start_s(self._cfg,
                                            self._cfg.trend_follow)):
                continue

            rnd = self._hydrated.get(raw.topic_id) or self._client.hydrate(raw)
            if rnd is None:
                LOG.debug("No strike yet for %s", raw.slug)
                continue
            self._hydrated[raw.topic_id] = rnd

            if self._cfg.min_liquidity > 0:
                if rnd.liquidity is None:
                    LOG.debug("Skipping %s: liquidity unknown and a minimum "
                              "is configured", rnd.slug)
                    continue
                if rnd.liquidity < self._cfg.min_liquidity:
                    LOG.debug("Skipping %s: liquidity %.0f below %.0f",
                              rnd.slug, rnd.liquidity, self._cfg.min_liquidity)
                    continue

            # Spot and volatility must come from the same series, or the
            # model is fed a price and a sigma describing different assets.
            symbol = self._client.market_symbol(rnd.feed_symbol)
            spot = self._market_data.spot(symbol)
            sigma = self._vol.sigma_annual(symbol)
            if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
                LOG.warning("Skipping %s: volatility clamped, so every edge "
                            "estimate would be unreliable", rnd.slug)
                continue
            tail_df = self._vol.tail_df(symbol)
            trend = self._vol.trend(symbol)
            book = {}
            for side in Side:
                levels = self._market_data.asks(rnd, side)
                if levels:
                    book[side] = levels

            # Size against uncommitted funds, never the full balance.
            verdict = assess(rnd, spot, sigma, available, now_ms, self._cfg,
                             book or None, tail_df, trend)
            if verdict.signal is None:
                # Deliberately NOT marked as seen. The round stays under
                # review for as long as it is live, because the price that
                # was too expensive a moment ago may not be in ten seconds --
                # writing a round off on its first look is how a return floor
                # turns into a bot that never trades.
                self._watching[rnd.topic_id] = (rnd.end_ms, verdict.blocked_by)
                LOG.debug("%s: %s (%.0fs left)", rnd.slug,
                          verdict.blocked_by, rnd.seconds_remaining(now_ms))
                continue
            sig = verdict.signal
            self._watching.pop(rnd.topic_id, None)

            if self._cfg.scale_in:
                # Open with a fraction of the target so there is room to add
                # if the round keeps going our way.
                first = max(sig.stake_usdt * self._cfg.scale_in_initial_pct,
                            self._cfg.min_stake_usdt)
                sig = replace(sig, stake_usdt=min(first, sig.stake_usdt))

            if self._cfg.entry_order_type == "LIMIT":
                # Expiry is fixed here, from the window that authorised THIS
                # order, so a later config change or a different strategy's
                # window cannot retroactively extend or shorten it.
                window_end_ms = rnd.end_ms - int(
                    self._cfg.entry_window_end_s * 1000)
                if self._post_limit_entry(rnd, sig, spot, sigma, bankroll,
                                          mode, window_end_ms):
                    self._seen[rnd.topic_id] = rnd.end_ms
                    available -= sig.stake_usdt
                    if available < self._cfg.min_stake_usdt:
                        return
                continue

            order_id = None
            if self._live:
                # Re-read the balance immediately before committing. The
                # figure from the top of the loop is seconds old and may
                # predate a settlement, a redemption landing, or a manual
                # withdrawal -- sizing from it can request more than the
                # account holds, which the venue rejects with -9000.
                fresh = self._live_bankroll("entry")
                if fresh is None:
                    continue
                if fresh < bankroll:
                    sig = self._resize(sig, rnd, fresh)
                    if sig is None:
                        continue
                if sig.stake_usdt > fresh:
                    LOG.warning("Stake %.2f exceeds the live balance %.2f; "
                                "skipping", sig.stake_usdt, fresh)
                    continue
                quote = self._client.get_quote(rnd, _market_buy(sig.side, sig.stake_usdt))

                # The quote is authoritative. Re-apply every price filter to it
                # and walk away if the venue prices worse than our screen
                # assumed -- the ceiling must bind on the executed price, not
                # merely on the order book we looked at a moment earlier.
                if quote.average_price > self._cfg.max_entry_price:
                    LOG.info("Quote %.4f above price ceiling %.2f; skipping",
                             quote.average_price, self._cfg.max_entry_price)
                    continue
                if not clears_return(quote.average_price, rnd.fee_bps,
                                     self._cfg):
                    LOG.info("Quote %.4f returns %.1f%% on a win, under the "
                             "%.0f%% floor; skipping",
                             quote.average_price,
                             win_return(quote.average_price, rnd.fee_bps) * 100,
                             self._cfg.min_win_return * 100)
                    continue
                if not clears_edge(sig.model_prob, quote.average_price,
                                   self._cfg, rnd.fee_bps):
                    LOG.info("Quote worse than screen (%.3f vs %.3f); skipping",
                             quote.average_price, sig.fill_price)
                    continue
                edge = (sig.model_prob
                        - breakeven_probability(quote.average_price,
                                                rnd.fee_bps))
                if abs(quote.price_impact) > self._cfg.max_price_impact:
                    LOG.info("Price impact %.1f%% too high; skipping",
                             quote.price_impact * 100)
                    continue

                order_id = self._client.place_order(rnd, quote,
                                                    sig.stake_usdt)
                # The order id alone proves nothing: PlaceOrderResponse has no
                # fill information, and a FOK order that cannot fill is killed
                # while still returning an id. Recording a position on that id
                # invents a trade, which then "settles" and books a profit
                # that was never made.
                if self._cfg.confirm_fills:
                    try:
                        filled = self._client.confirm_fill(order_id,
                                                           sig.stake_usdt)
                    except (ApiError, requests.RequestException) as exc:
                        LOG.error("NOT recording a position for %s: %s",
                                  rnd.slug, exc)
                        self._seen[rnd.topic_id] = rnd.end_ms
                        continue
                    if abs(filled - sig.stake_usdt) > EPS:
                        LOG.warning("Filled %.4f of %.4f requested on %s; "
                                    "tracking the filled amount",
                                    filled, sig.stake_usdt, rnd.slug)
                        sig = replace(sig, stake_usdt=filled)
                if quote.fee_usdt > 0:
                    LOG.info("Venue fee %.4f USDT (%.0f bps of stake)",
                             quote.fee_usdt,
                             quote.fee_usdt / sig.stake_usdt * 10_000)
                sig = replace(sig, fill_price=quote.average_price, edge=edge)
                LOG.info("Order %s filled at %.4f for %.4f shares", order_id,
                         quote.average_price, quote.amount_out)

            mult = kelly_multiple(sig.stake_usdt, bankroll, sig.model_prob,
                                  sig.fill_price, rnd.fee_bps)
            mult_s = "" if mult is None else f" [{mult:.2f}x Kelly]"
            if mult is not None and mult > 1.0:
                LOG.warning("Staking %.2fx the full-Kelly fraction because the "
                            "venue minimum exceeds the Kelly size on a %.2f "
                            "bankroll", mult, bankroll)
            LOG.info("ENTER %s %s | fill %.3f model %.3f edge %+.3f "
                     "pays %+.0f%% stake %.2f%s (%.0fs left)%s",
                     rnd.slug, sig.side.value,
                     sig.fill_price, sig.model_prob, sig.edge,
                     win_return(sig.fill_price, rnd.fee_bps) * 100,
                     sig.stake_usdt, mult_s, sig.seconds_left,
                     f"  TREND {trend.describe()}" if sig.trend_boosted
                     else "")

            tid = self._journal.record(mode, rnd, sig, spot, sigma, bankroll,
                                       order_id)
            self._seen[rnd.topic_id] = rnd.end_ms
            self._positions[(rnd.symbol, sig.side)] = Position(
                tid, rnd, sig, sig.stake_usdt, 1)
            available -= sig.stake_usdt
            if (len(self._positions) >= self._cfg.max_concurrent_positions
                    or available < self._cfg.min_stake_usdt):
                return
