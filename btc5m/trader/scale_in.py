"""
Adding to a position that is still cheap, without letting the blended
average cross the price that clears the edge.
"""

from __future__ import annotations

from dataclasses import replace

import requests

from btc5m.assessment import blended_price_cap, clears_edge, clears_return
from btc5m.constants import EPS, LOG
from btc5m.domain import Side, _market_buy
from btc5m.errors import ApiError
from btc5m.pnl import wins_per_loss
from btc5m.pricing import digital_up_probability
from btc5m.sizing import (
    boosted_stake,
    kelly_stake,
    max_topup_within_blend,
    walk_book,
)


class ScaleInMixin:
    """Scaling into an open position."""
    def _maybe_scale_in_all(self, bankroll: float) -> None:
        if self._cfg.last_minute or self._cfg.scalp:
            # last_minute: one order, one round, held to settlement.
            # scalp: a top-up would move the fill price the bracket was
            # already computed from, leaving the stop guarding a level
            # neither tranche chose.
            #
            # Config rejects both pairings outright; this is the belt to that
            # brace, so a future caller cannot route around the validation.
            return
        if self._cfg.straddle:
            # Both legs are bought once, at round-open, and left alone until
            # settlement -- no top-up, no exit, nothing sold mid-round. This
            # profile has no model probability for scale-in to top up
            # toward, which is also why straddle+scale_in cannot both be
            # enabled (see Config.__post_init__).
            return
        for key in list(self._positions):
            self._maybe_scale_in(bankroll, key)

    def _maybe_scale_in(self, bankroll: float,
                        key: tuple[str, Side] | None = None) -> None:
        """
        Top up an open position as the round moves further into our favour.

        The target is the Kelly stake for the CURRENT probability. If the
        buffer has grown, the target grows, and we add the difference. If the
        round has turned against us the target falls and we add nothing --
        adding there would be chasing a loser, which is the failure this
        deliberately avoids.

        Because the target is recomputed rather than accumulated, total
        exposure to one round stays bounded by Kelly no matter how many
        tranches are added.
        """
        pos = (self._positions.get(key) if key is not None
               else self._position)
        if pos is None or not self._cfg.scale_in:
            return
        key = (pos.rnd.symbol, pos.signal.side)
        secs = pos.rnd.seconds_remaining(self._client.now_ms())
        if secs <= self._cfg.entry_window_end_s:
            return                       # too late to fill

        symbol = self._client.market_symbol(pos.rnd.feed_symbol)
        spot = self._market_data.spot(symbol)
        sigma = self._vol.sigma_annual(symbol)
        if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
            return
        tail_df = self._vol.tail_df(symbol)

        if pos.rnd.strike is None:
            return
        p_up = digital_up_probability(spot, pos.rnd.strike, sigma, secs, tail_df)
        prob = p_up if pos.signal.side is Side.UP else 1.0 - p_up
        if prob <= pos.signal.model_prob:
            return                       # not more favourable than before

        levels = self._market_data.asks(pos.rnd, pos.signal.side)
        if not levels:
            return
        price = levels[0][0]
        if not (self._cfg.min_entry_price <= price <= self._cfg.max_entry_price):
            return
        if not clears_return(price, pos.rnd.fee_bps, self._cfg):
            return
        if not clears_edge(prob, price, self._cfg, pos.rnd.fee_bps):
            return

        target = kelly_stake(bankroll + pos.committed_usdt, prob, price,
                             self._cfg, pos.rnd.fee_bps)
        if pos.signal.trend_boosted:
            # The position was opened at trend size; topping up to the plain
            # Kelly target would shrink it back mid-round, which is neither
            # the trend rule nor the Kelly rule but an accident of applying
            # one at entry and the other afterwards.
            target = boosted_stake(target, bankroll + pos.committed_usdt,
                                   prob, price, self._cfg, pos.rnd.fee_bps)
        topup = target - pos.committed_usdt
        floor = max(self._cfg.scale_in_min_topup, self._cfg.min_stake_usdt)
        if topup < floor:
            return

        # Trim so the blended fill stays under the ceiling. Without this a
        # top-up at a high price silently converts a position that needed six
        # wins per loss into one needing fifteen.
        cap = blended_price_cap(self._cfg, pos.rnd.fee_bps)
        allowed = max_topup_within_blend(
            pos.committed_usdt, pos.signal.fill_price, price, cap)
        if allowed < floor:
            LOG.debug("No top-up for %s: blended price would exceed %.3f",
                      pos.rnd.slug, cap)
            return
        if topup > allowed:
            LOG.info("Trimming top-up %.2f -> %.2f to hold the blended price "
                     "under %.3f", topup, allowed, cap)
            topup = allowed
        avg = walk_book(levels, topup)
        if avg is None or not clears_edge(prob, avg, self._cfg, pos.rnd.fee_bps):
            return
        if not clears_return(avg, pos.rnd.fee_bps, self._cfg):
            return

        if self._live:
            fresh = self._live_bankroll("scale-in")
            if fresh is None or topup > fresh:
                LOG.info("Skipping top-up: %.2f needed, %.2f available",
                         topup, fresh if fresh is not None else -1.0)
                return
            quote = self._client.get_quote(pos.rnd, _market_buy(pos.signal.side, topup))
            if quote.average_price > self._cfg.max_entry_price:
                return
            if not clears_return(quote.average_price, pos.rnd.fee_bps,
                                 self._cfg):
                return
            if pos.average_price(topup, quote.average_price) > cap + EPS:
                # The trim above was computed against the book; the venue's
                # executable price can be worse, and the blend is what pays.
                LOG.info("Top-up quote %.4f would blend past %.3f; skipping",
                         quote.average_price, cap)
                return
            if not clears_edge(prob, quote.average_price, self._cfg,
                               pos.rnd.fee_bps):
                return
            if abs(quote.price_impact) > self._cfg.max_price_impact:
                return
            topup_order = self._client.place_order(pos.rnd, quote, topup)
            if self._cfg.confirm_fills:
                try:
                    filled = self._client.confirm_fill(topup_order, topup)
                except (ApiError, requests.RequestException) as exc:
                    # The opener stands; only the top-up failed. Adding it to
                    # the position would overstate exposure on a fill that
                    # never happened.
                    LOG.error("Top-up on %s not confirmed, leaving the "
                              "position unchanged: %s", pos.rnd.slug, exc)
                    return
                if abs(filled - topup) > EPS:
                    LOG.warning("Top-up filled %.4f of %.4f on %s",
                                filled, topup, pos.rnd.slug)
                    topup = filled
            if quote.fee_usdt > 0:
                LOG.debug("Top-up fee %.4f USDT on %.2f staked",
                          quote.fee_usdt, topup)
            avg = quote.average_price

        blended = pos.average_price(topup, avg)
        LOG.info("SCALE-IN %s +%.2f at %.3f (prob %.3f, %.0fs left) -> "
                 "committed %.2f, blended %.3f, %.1f wins per loss",
                 pos.rnd.slug, topup, avg, prob, secs,
                 pos.committed_usdt + topup, blended, wins_per_loss(blended))

        self._positions[key] = replace(
            pos,
            signal=replace(pos.signal, model_prob=prob, fill_price=blended,
                           stake_usdt=pos.committed_usdt + topup),
            committed_usdt=pos.committed_usdt + topup,
            tranches=pos.tranches + 1)
