"""
A resting order from placement to fill, expiry or retraction. One
seam, so paper and live share the whole lifecycle.
"""

from __future__ import annotations

from dataclasses import replace

import requests

from btc5m.constants import EPS, LOG
from btc5m.domain import (
    Action,
    OrderPlan,
    OrderType,
    PendingOrder,
    Position,
    _market_buy,
)
from btc5m.errors import ApiError, OrderNotFilled
from btc5m.pricing import buy_reservation_price

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import OrderState, Round, Side, Signal


class OrderLifecycleMixin:
    """Placing, reaping, retracting and booking resting orders."""
    def _book_price(self, raw: Round, side: Side) -> float | None:
        """
        Best ask for one side, or None when there is no usable price.

        None is not an error, it is the ordinary state of a market late in a
        round: the side that is losing decays toward 0.00 and the side that
        is winning toward 1.00, and rounding to the market's own precision
        snaps both the rest of the way. Neither is a probability -- 1.00 can
        never pay back more than it cost and 0.00 has no payout defined at
        all -- so every price formula downstream rejects them, and the one
        in _complete_half_straddles rejected them by raising ValueError out
        of breakeven_probability and killing the process.

        Returning None instead says the same thing without pretending the
        number is tradable, and without clamping it into range, which would
        quietly turn "this side is worthless" into "this side is a bargain".
        """
        price = raw.round_price(self._raw_book_price(raw, side))
        return price if 0.0 < price < 1.0 else None

    def _raw_book_price(self, raw: Round, side: Side) -> float:
        """
        Best ask for one side, before rounding and before the usability test.

        Split out from _book_price because RANKING the two sides and BUYING
        one of them are different questions. A side that has decayed to 0.004
        is not tradable and _book_price is right to answer None, but it is
        still unambiguously the cheaper of the two -- and a caller asking
        "which side is the book calling the winner?" needs that answer, not a
        None it would have to guess the direction of.
        """
        levels = self._client.asks_for(raw, side)
        return (levels[0][0] if levels
                else min(raw.quote_for(side)
                         * (1.0 + self._cfg.assumed_spread_pct), 0.999))

    def _place_leg(self, raw: Round, side: Side, price: float, stake: float,
                   quote: "Quote | None" = None
                   ) -> tuple[float, float, str | None] | None:
        """
        Buy one side. Returns (fill price, stake filled, order id), or None.

        None means the venue said the order did not fill, so there is no
        position and the caller must not record one. A MARKET FOK order that
        cannot fill is killed while still returning an order id, so the id
        alone proves nothing.

        The two confirmation failures are deliberately NOT treated alike. A
        dead status is knowledge: no position, nothing to record, and
        recording one would invent a trade that later settles and books a
        profit never made. A timeout or a dropped socket is the absence of
        knowledge: the order may well be live, and dropping it strands real
        money that is never settled and never claimed. So the first returns
        None and the second records at the requested size, loudly.

        Paper mode fills at the passed price and places nothing.
        """
        order_id = None
        if self._live:
            if quote is None:
                quote = self._client.get_quote(raw, _market_buy(side, stake))
            order_id = self._client.place_order(raw, quote, stake)
            price = quote.average_price
            if self._cfg.confirm_fills:
                try:
                    stake = self._client.confirm_fill(order_id, stake)
                    # What the venue executed at, not what the quote
                    # estimated. Live, a buy quoted at 0.49 executed at 0.36;
                    # kept at 0.49, its stop sat above the real entry and
                    # fired on a position that was up.
                    executed = self._client.executed_price(order_id)
                    if executed is not None:
                        if executed != price:
                            LOG.info("%s: %s executed at %.4f; quoted %.4f",
                                     raw.slug, side.value, executed, price)
                        price = executed
                except OrderNotFilled as exc:
                    LOG.warning("%s: the %s order did not fill (%s); no "
                                "position recorded", raw.slug, side.value,
                                exc)
                    return None
                except (ApiError, requests.RequestException) as exc:
                    LOG.error("%s: the %s order was placed but the fill could "
                              "NOT be confirmed either way (%s); recording it "
                              "at the requested %.2f USDT so it is settled "
                              "and claimed rather than stranded",
                              raw.slug, side.value, exc, stake)
            if quote.fee_usdt > 0:
                LOG.debug("%s %s fee %.4f USDT", raw.slug,
                          side.value, quote.fee_usdt)
        return price, stake, order_id

    def _cancel_all_pending(self) -> None:
        """
        Retract every resting order, then book whatever filled first.

        Delegates the cancelling to the reaper rather than doing its own
        pass. Cancelling here and then reaping sent every order to
        batch-cancel twice, and a second cancel of an order that filled in
        between reads as a fresh failure for a reason that no longer exists.
        """
        if not self._pending:
            return
        LOG.info("Cancelling %d resting order(s)", len(self._pending))
        self._reap_pending(force_final=True)

    def _reap_pending(self, force_final: bool = False) -> None:
        """
        Advance every resting order: book fills, retract what has run out.

        Called at the top of the loop, before settlement, because a fill has
        to become a position before its round settles.

        The cancel is a request, not an answer. batch-cancel reports an order
        under `failed` most often because it FILLED first, so nothing here
        reads that list: after any cancel the order's state is read again and
        whatever came back is booked. Treating a failed cancel as "still
        resting" walks away from a real position, which then settles, wins,
        and is never claimed because no journal row knows it exists.
        """
        if not self._pending:
            return
        now_ms = self._client.now_ms()
        for pending in list(self._pending.values()):
            # The order id travels ON the order rather than only as the dict
            # key, so a helper that is handed a PendingOrder can name the
            # order it is talking about without the caller passing it too.
            order_id = pending.order_id
            try:
                state = self._orders.order_state(order_id)
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("Could not read order %s: %s", order_id, exc)
                continue
            if state is None:
                # The venue has no record yet. That is the absence of
                # knowledge, not death: the order may well be live, and
                # dropping it here strands it.
                continue

            pending = self._book_fill(order_id, pending, state)
            if state.status in ("FILLED", "DEAD"):
                self._pending.pop(order_id, None)
                continue

            expired = (now_ms >= pending.expires_at_ms
                       or now_ms >= pending.rnd.end_ms
                       or force_final)
            if not expired:
                continue

            self._retract(order_id,
                          "round over" if now_ms >= pending.rnd.end_ms
                          else "entry window closed")

    def _retract(self, order_id: str, reason: str) -> bool:
        """
        Cancel one resting order and book whatever the cancel raced.

        True when the order is done with and out of _pending; False when it
        could not be reached, in which case it is left pending and retried.

        The cancel is a request, not an answer. batch-cancel reports an order
        under `failed` most often because it FILLED first, so nothing here
        reads that list: the order's state AFTER the cancel is the answer,
        and whatever came back is booked. Reading `failed` as "still
        resting" walks away from a real position, which then settles, wins,
        and is never claimed because no journal row knows it exists.

        One body rather than two, because the reaper and the stop trigger
        need exactly the same discipline and the stop is the case where the
        race is most likely -- it fires precisely when the price is moving.
        """
        pending = self._pending.get(order_id)
        if pending is None:
            return True
        LOG.info("%s: retracting the %s %s order (%s)", pending.rnd.slug,
                 pending.plan.side.value, pending.plan.action.value, reason)
        try:
            self._orders.cancel_orders([order_id])
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Cancel failed for %s: %s", order_id, exc)
            return False
        try:
            final = self._orders.order_state(order_id)
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Could not re-read %s after cancel: %s", order_id, exc)
            return False
        if final is not None:
            self._book_fill(order_id, self._pending.get(order_id, pending),
                            final)
        self._pending.pop(order_id, None)
        return True

    def _book_fill(self, order_id: str, pending: PendingOrder,
                   state: OrderState) -> PendingOrder:
        """
        Record whatever this order has filled that is not recorded yet.

        min_fill_fraction is deliberately not consulted. It is the FOK guard,
        where a short fill means something went wrong; on a GTC order a short
        fill is the ordinary outcome, and refusing it strands shares the
        account already holds. Any non-zero fill becomes a position.
        """
        if pending.plan.action is Action.SELL:
            return self._book_sale(order_id, pending, state)
        new_usdt = state.filled_usdt - pending.filled_usdt
        if new_usdt <= EPS:
            return pending
        price = (state.price or pending.fill_price
                 or pending.plan.price_limit or 0.0)
        key = (pending.rnd.symbol, pending.plan.side)
        existing = self._positions.get(key)
        trade_id = pending.trade_id
        if existing is None:
            sig = replace(pending.signal, stake_usdt=state.filled_usdt,
                          fill_price=price)
            if trade_id is None:
                trade_id = self._journal.record(
                    "LIVE" if self._live else "PAPER", pending.rnd, sig,
                    self._market_data.spot(pending.rnd.symbol),
                    self._vol.sigma_annual(pending.rnd.symbol),
                    self._bankroll(), order_id,
                    order_type=pending.plan.order_type.value,
                    price_limit=pending.plan.price_limit)
            self._positions[key] = Position(trade_id, pending.rnd, sig,
                                            state.filled_usdt, 1)
        else:
            blended = existing.average_price(new_usdt, price)
            self._positions[key] = replace(
                existing,
                signal=replace(existing.signal, fill_price=blended,
                               stake_usdt=state.filled_usdt),
                committed_usdt=state.filled_usdt,
                tranches=existing.tranches + 1)
            trade_id = existing.trade_id
        LOG.info("%s: %s order filled %.4f USDT at %.4f (%.4f of %.4f)",
                 pending.rnd.slug, pending.plan.side.value, new_usdt, price,
                 state.filled_usdt, pending.plan.amount)
        updated = replace(pending, filled_usdt=state.filled_usdt,
                          filled_shares=state.filled_shares,
                          trade_id=trade_id)
        self._pending[order_id] = updated
        return updated

    def _post_limit_entry(self, rnd: Round, sig: Signal, spot: float,
                          sigma: float, bankroll: float, mode: str,
                          expires_at_ms: int) -> bool:
        """
        Post a resting bid at the model's reservation price. True if accepted.

        No position is recorded here. The order is on the book and nothing has
        filled; recording one now books a trade that may never happen, which
        then "settles" and reports a result that was never real.
        """
        price = buy_reservation_price(sig.model_prob, self._cfg, rnd.fee_bps)
        if price is None:
            LOG.info("%s: no price in the band clears the gates; not posting",
                     rnd.slug)
            return False
        price = rnd.round_price(price)
        if not 0.0 < price < 1.0:
            return False
        plan = OrderPlan(side=sig.side, action=Action.BUY,
                         order_type=OrderType.LIMIT, amount=sig.stake_usdt,
                         price_limit=price)
        if self._live:
            try:
                quote = self._client.get_quote(rnd, plan)
                order_id = self._client.place_order(rnd, quote,
                                                    sig.stake_usdt)
            except (OrderNotFilled, ApiError,
                    requests.RequestException) as exc:
                LOG.warning("%s: limit entry rejected: %s", rnd.slug, exc)
                return False
        else:
            order_id = self._paper_book.place(plan, rnd)
        self._pending[str(order_id)] = PendingOrder(
            order_id=str(order_id), rnd=rnd, plan=plan,
            signal=replace(sig, fill_price=price),
            expires_at_ms=expires_at_ms,
            filled_usdt=0.0, filled_shares=0.0, trade_id=None)
        LOG.info("POST %s %s | limit %.4f model %.3f stake %.2f (%.0fs left)",
                 rnd.slug, sig.side.value, price, sig.model_prob,
                 sig.stake_usdt, sig.seconds_left)
        return True
