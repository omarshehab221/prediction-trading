"""
The hybrid profile: a locked straddle when a round offers one, a buffer
entry when it does not, and a stop under every buffer position.
"""

from __future__ import annotations

import math
from dataclasses import replace

from btc5m.assessment import assess, clears_edge, clears_return
from btc5m.constants import LOG
from btc5m.domain import Bracket, Position, _market_buy
from btc5m.pricing import breakeven_probability, win_return
from btc5m.sizing import walk_book


class HybridMixin:

    def _maybe_enter_hybrid(self, bankroll: float, mode: str) -> None:
        """
        Straddle, then the hedge, then the model -- in that order, every pass.

        Order is the whole priority rule. The straddle path completes open
        legs and opens new ones; anything it opens is a position on its
        symbol, and the model path already refuses a symbol with a position,
        so a market holds one strategy's bet per round and never both. Both
        paths size against _available, which counts what the straddle just
        committed, so the model cannot spend the same money twice.
        """
        self._maybe_enter_straddle(bankroll, mode)
        self._hedge_stranded_legs(bankroll, mode)
        self._maybe_enter_model(bankroll, mode)

    def _hedge_stranded_legs(self, bankroll: float, mode: str) -> None:
        """
        Hedge a straddle leg whose partner never came -- strategically.

        The straddle path only completes a round at a price that locks it,
        and a leg left alone rides to settlement as a directional bet. When
        the market has all but decided against that leg, riding it is a
        near-certain loss. This buys the other side then, while the buffer
        is large, the window is open and the price still pays -- not at the
        deadline, where force hedging paid whatever was left.

        Every buffer gate applies to the hedge side (band, return floor,
        edge), with the buffer raised to hybrid_hedge_min_sigmas. A price
        that would LOCK the round is left to the straddle path, which owns
        completions and runs first.

        Sized so a win pays back the stranded leg's stake, capped at
        hybrid_hedge_max_stake_pct of bankroll and floored at the venue
        minimum; a capped hedge still covers part of the loss.
        """
        if not self._cfg.hybrid:
            return
        now_ms = self._client.now_ms()
        strict = replace(self._cfg, min_buffer_sigmas=max(
            self._cfg.min_buffer_sigmas, self._cfg.hybrid_hedge_min_sigmas))
        for (symbol, side), pos in list(self._positions.items()):
            if pos.trade_id not in self._hybrid_straddle_legs:
                continue
            other = side.other
            if (symbol, other) in self._positions:
                continue
            raw = pos.rnd
            if ((now_ms - raw.start_ms) / 1000.0
                    <= self._cfg.straddle_entry_window_s):
                continue
            levels = self._market_data.asks(raw, other)
            if not levels:
                continue
            ask = raw.round_price(levels[0][0])
            if not 0.0 < ask < 1.0:
                continue
            if (breakeven_probability(pos.signal.fill_price, raw.fee_bps)
                    + breakeven_probability(ask, raw.fee_bps) < 1.0):
                # That price locks the round: a completion, not a hedge.
                continue

            rnd = self._hydrated.get(raw.topic_id) or self._client.hydrate(raw)
            if rnd is None or rnd.strike is None:
                continue
            self._hydrated[raw.topic_id] = rnd
            feed = self._client.market_symbol(rnd.feed_symbol)
            if (self._cfg.halt_on_clamped_sigma
                    and self._vol.is_clamped(feed)):
                continue
            spot = self._market_data.spot(feed)
            sigma = self._vol.sigma_annual(feed)
            # No trend: the hedge window is buffer's plain one, and a trend
            # widening it would reach back into the straddle minute.
            verdict = assess(rnd, spot, sigma, bankroll, now_ms, strict,
                             {other: levels}, self._vol.tail_df(feed), None)
            sig = verdict.signal
            if sig is None or sig.side is not other:
                continue

            cover = pos.committed_usdt / win_return(sig.fill_price,
                                                    rnd.fee_bps)
            stake = min(max(cover, self._cfg.min_stake_usdt),
                        bankroll * self._cfg.hybrid_hedge_max_stake_pct,
                        self._available(bankroll))
            if stake < self._cfg.min_stake_usdt:
                LOG.debug("%s: hedge for %s wants %.2f; only %.2f can be "
                          "staked", rnd.slug, side.value, cover, stake)
                continue
            avg = walk_book(levels, stake)
            if avg is None:
                continue
            price = rnd.round_price(avg)
            if not (self._cfg.min_entry_price <= price
                    <= self._cfg.max_entry_price
                    and clears_return(price, rnd.fee_bps, self._cfg)
                    and clears_edge(sig.model_prob, price, self._cfg,
                                    rnd.fee_bps)):
                continue

            quote = None
            if self._live:
                fresh = self._live_bankroll("hedge")
                if fresh is None or fresh < stake:
                    continue
                quote = self._client.get_quote(rnd, _market_buy(other, stake))
                price = quote.average_price
                if not (self._cfg.min_entry_price <= price
                        <= self._cfg.max_entry_price
                        and clears_return(price, rnd.fee_bps, self._cfg)
                        and clears_edge(sig.model_prob, price, self._cfg,
                                        rnd.fee_bps)):
                    LOG.info("%s: hedge quote %.4f fails the buffer gates; "
                             "not hedging", rnd.slug, price)
                    continue
            placed = self._place_leg(rnd, other, price, stake, quote)
            if placed is None:
                continue
            price, stake, order_id = placed
            sig = replace(sig, fill_price=price, stake_usdt=stake,
                          edge=sig.model_prob
                          - breakeven_probability(price, rnd.fee_bps))
            tid = self._journal.record(mode, rnd, sig, spot, sigma, bankroll,
                                       order_id)
            shares = (self._client.delivered_shares(order_id)
                      if self._live and order_id else None)
            self._positions[(symbol, other)] = Position(tid, rnd, sig, stake,
                                                        1, shares=shares)
            # Paired now: neither side is stopped or scaled into.
            self._hybrid_straddle_legs.add(tid)
            wins = stake * win_return(price, rnd.fee_bps)
            LOG.info("HEDGE %s | %s %.4f (%.2f) against the stranded %s leg "
                     "(%.2f at %.4f) | buffer %.2f sigmas, model %.3f | a "
                     "hedge win returns %+.2f against the leg's %.2f "
                     "(%.0fs left)", rnd.slug, other.value, price, stake,
                     side.value, pos.committed_usdt, pos.signal.fill_price,
                     sig.buffer_z, sig.model_prob, wins, pos.committed_usdt,
                     rnd.seconds_remaining(now_ms))

    def _sync_hybrid_stops(self) -> None:
        """
        Make the brackets say what the positions are, every pass.

        Declarative rather than armed at each entry point, because a buffer
        entry and each of its top-ups change what a stop should be, and
        arming at each is several places for one to be forgotten. The rule:
        a buffer position carries a stop; a straddle leg never does.

        Straddle legs were stopped too, until the 2026-09-16 shadow session:
        nine unpaired first legs were stopped 20-60s after entry for -5.33
        USDT while the two pairs that completed made +1.43. A cheap first
        leg falling is the strategy waiting for its partner, not failing,
        and selling it forecloses the completion. A leg that never finds
        its partner rides to settlement, as it does in the straddle profile.
        """
        if not self._cfg.hybrid:
            return
        held = {pos.trade_id for pos in self._positions.values()}
        self._hybrid_stopped &= held
        self._hybrid_straddle_legs &= held
        for key, pos in self._positions.items():
            if pos.trade_id in self._hybrid_straddle_legs:
                self._brackets.pop(key, None)
                continue
            if pos.trade_id in self._hybrid_stopped:
                continue
            entry = pos.signal.fill_price
            current = self._brackets.get(key)
            if current is not None and current.entry_price == entry:
                continue
            stop = pos.rnd.round_price(
                entry * (1.0 - self._cfg.hybrid_stop_loss_pct))
            # No take-profit: winners ride to settlement. An infinite target
            # is one _check_stops' ">=" can never reach.
            self._brackets[key] = Bracket(entry_price=entry, tp_price=math.inf,
                                          stop_price=stop)
