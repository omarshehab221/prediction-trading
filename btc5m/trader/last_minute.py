"""Buying a favourite late, when the buffer is large in the time left."""

from __future__ import annotations

import math

from btc5m.constants import LOG
from btc5m.domain import Position, Side, Signal, _market_buy
from btc5m.errors import TradingHalted
from btc5m.pricing import breakeven_probability, win_return


class LastMinuteMixin:
    """The last-minute strategy."""
    def _maybe_enter_last_minute(self, bankroll: float, mode: str) -> None:
        """
        Buy whichever side the book has already picked, as the clock runs out.

        One rule, and nothing underneath it. No model probability, no edge
        test, no buffer, no trend, no volatility -- none of it is computed,
        let alone consulted. With last_minute_start_s left in the round, read
        the two asks, take the DEARER one, and stop.

        The floor and the fallback are one rule in two halves, not a rule and
        an excuse:

          * above last_minute_fallback_s the leader must show
            last_minute_price_floor. A leader under it means the round is
            still a genuine contest, and there is time left for it to stop
            being one, so nothing is bought yet.
          * at or below last_minute_fallback_s that time has run out, and
            "no side reached 0.75" has itself become the answer: the round
            IS close, the leader is the best read anyone has of how it will
            land, and -- because it failed the floor -- it is cheap. So it
            is bought at whatever it costs.

        The dear end is where this can bleed, and the dear end is the FIRST
        branch, not the second. By default nothing caps the price above the
        floor, so a round already decided at 55 seconds quotes 0.97 and gets
        bought for about 3% on a win, where it takes 32 wins to cover one
        loss -- against 3 wins at the 0.75 floor. That is the profile as
        specified, and last_minute_max_price is the one knob that changes it:
        set it to 0.90 or 0.85 and those rounds are refused instead. It is
        left at 1.0 by default because refusing them is a decision about
        which trades the strategy is for, not a bug fix. The
        favourite-longshot table in --calibration-report is what says whether
        the venue's late favourites win often enough to pay for them.

        Two rounds are left alone, and neither is a judgement about price:
        one where the sides are quoted level, because there is no dearer side
        to buy and resolving that with a coin flip on UP would be inventing a
        signal; and one where the leader has rounded to 1.00, because a
        contract at 1.00 cannot pay back more than it cost, so buying it is
        a fee with extra steps.

        Everything the other strategies share still applies: the daily loss
        limit, the calibration breaker, the streak cap, the reserve, the
        concurrency cap and fill confirmation. Those are not strategy, they
        are the difference between a bot that is losing and one that has
        stopped.
        """
        now_ms = self._client.now_ms()
        self._prune(now_ms)

        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        target = bankroll * self._cfg.last_minute_stake_pct
        # Two different conditions, and conflating them is what once hid the
        # same bug on the straddle path. Sizing under the venue minimum is a
        # configuration fault that will never clear on its own, so it is loud
        # and said once. Capital tied up in another market is the ordinary
        # state of a profile holding a position, so it stays quiet.
        if target < self._cfg.min_stake_usdt:
            msg = ("The last-minute profile cannot enter any round: %.2f "
                   "USDT (%.1f%% of a %.2f bankroll) is below the %.2f venue "
                   "minimum. Raise last_minute_stake_pct or fund the wallet "
                   "-- nothing will be traded until one of those changes."
                   % (target, self._cfg.last_minute_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt))
            if msg != self._idle_reason:
                self._idle_reason = msg
                LOG.warning("%s", msg)
            return
        self._idle_reason = ""

        if self._available(bankroll) < self._cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a last-minute entry "
                      "(%.2f committed of %.2f, %.0f%% reserved)",
                      self._committed(), bankroll,
                      self._cfg.reserve_pct * 100)
            return

        for raw in self._list_rounds():
            if raw.topic_id in self._seen:
                continue
            # One position per market: a second on the same symbol is the
            # same bet twice, not diversification.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue

            secs = raw.seconds_remaining(now_ms)
            if secs > self._cfg.last_minute_start_s:
                # Deliberately NOT marked seen. Its minute has not come yet.
                continue
            if secs <= self._cfg.last_minute_deadline_s:
                # Out of time. Whatever reason was last recorded against this
                # round is the informative one, so it is left standing rather
                # than overwritten with the clock running out -- that is a
                # consequence of the real reason, not the reason.
                self._watching.setdefault(
                    raw.topic_id,
                    (raw.end_ms,
                     "the last minute ran out with no side to buy"))
                self._seen[raw.topic_id] = raw.end_ms
                continue

            asks = {side: self._raw_book_price(raw, side)
                    for side in (Side.UP, Side.DOWN)}
            if asks[Side.UP] == asks[Side.DOWN]:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the two sides are priced level")
                continue
            side = max(asks, key=asks.get)
            price = self._book_price(raw, side)
            if price is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the leading side is priced at 1.00")
                continue
            if price > self._cfg.last_minute_max_price:
                # Above the ceiling there is too little left to win for the
                # whole stake it risks. Unlike the floor this is never
                # relaxed by the clock: a round that is already decided does
                # not become a better bet for being nearly over.
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the leading side is priced above the ceiling")
                continue
            if (price < self._cfg.last_minute_price_floor
                    and secs > self._cfg.last_minute_fallback_s):
                self._watching[raw.topic_id] = (
                    raw.end_ms, "no side has reached the price floor")
                continue

            # Recomputed per round, not once per pass: entering one round
            # commits capital that the next round in this pass must not be
            # sized against as though it were still free.
            stake = min(target, self._available(bankroll))
            if stake < self._cfg.min_stake_usdt:
                LOG.debug("%s: %.2f left after the reserve and %.2f already "
                          "committed, under the %.2f minimum", raw.slug,
                          stake, self._committed(), self._cfg.min_stake_usdt)
                continue

            quote = None
            if self._live:
                # Re-read the balance immediately before committing. The
                # figure from the top of the loop is seconds old and may
                # predate a settlement, a redemption landing or a manual
                # withdrawal; sizing from it can ask for more than the
                # account holds, which the venue rejects with -9000.
                fresh = self._live_bankroll("last-minute entry")
                if fresh is None:
                    continue
                stake = min(stake, fresh)
                if stake < self._cfg.min_stake_usdt:
                    LOG.warning("%s: the wallet holds %.2f, under the %.2f "
                                "minimum order; skipping", raw.slug, fresh,
                                self._cfg.min_stake_usdt)
                    continue
                quote = self._client.get_quote(raw, _market_buy(side, stake))
                # The quote is authoritative and the book was only a screen.
                # Re-test the rule on the price that will actually execute,
                # or the floor binds on a number nobody pays.
                if not 0.0 < quote.average_price < 1.0:
                    LOG.info("%s: the %s quote came back at %.4f, which has "
                             "no payout; skipping", raw.slug, side.value,
                             quote.average_price)
                    continue
                if quote.average_price > self._cfg.last_minute_max_price:
                    LOG.info("%s: the %s quote at %.4f is above the %.2f "
                             "ceiling the book suggested it would clear; "
                             "skipping", raw.slug, side.value,
                             quote.average_price,
                             self._cfg.last_minute_max_price)
                    self._watching[raw.topic_id] = (
                        raw.end_ms,
                        "the leading side is priced above the ceiling")
                    continue
                if (quote.average_price < self._cfg.last_minute_price_floor
                        and secs > self._cfg.last_minute_fallback_s):
                    LOG.info("%s: the %s quote at %.4f is under the %.2f "
                             "floor with %.0fs left; waiting", raw.slug,
                             side.value, quote.average_price,
                             self._cfg.last_minute_price_floor, secs)
                    self._watching[raw.topic_id] = (
                        raw.end_ms, "no side has reached the price floor")
                    continue
                price = quote.average_price

            self._watching.pop(raw.topic_id, None)
            self._seen[raw.topic_id] = raw.end_ms
            placed = self._place_leg(raw, side, price, stake, quote)
            if placed is None:
                continue              # killed by the venue; nothing opened
            price, stake, order_id = placed

            # model_prob is the MARKET'S implied probability, not a forecast
            # of ours -- this strategy makes none. Recording the price
            # restated as a probability is what makes the calibration breaker
            # mean something here: it then asks "are the favourites I am
            # buying winning as often as I paid for them to?", which is the
            # one health question this profile has, and halts if they are
            # not. A neutral 0.5 would have left that test permanently and
            # uninformatively positive. edge is 0.0 for the same reason:
            # paying the market price is by definition no edge over it.
            implied = breakeven_probability(price, raw.fee_bps)
            sig = Signal(side, model_prob=implied, fill_price=price,
                         edge=0.0, stake_usdt=stake,
                         seconds_left=raw.seconds_remaining(now_ms))
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id)
            self._positions[(raw.symbol, side)] = Position(
                tid, raw, sig, stake, 1)
            LOG.info("LAST MINUTE %s | %s %.4f (%.2f) implied %.1f%% "
                     "pays %+.0f%% (%.0fs left)%s", raw.slug, side.value,
                     price, stake, implied * 100,
                     win_return(price, raw.fee_bps) * 100, secs,
                     "  [floor dropped]"
                     if price < self._cfg.last_minute_price_floor else "")

            if (len(self._positions) >= self._cfg.max_concurrent_positions
                    or self._available(bankroll) < self._cfg.min_stake_usdt):
                return
