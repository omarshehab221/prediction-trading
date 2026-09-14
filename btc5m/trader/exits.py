"""Leaving a position before the oracle decides it."""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

import requests

from btc5m.constants import EPS, LOG, SHARE_PRECISION
from btc5m.domain import Action, OrderPlan, OrderType, PendingOrder, Side
from btc5m.errors import ApiError
from btc5m.pricing import digital_up_probability, sell_reservation_price

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import OrderState, Position


class ExitsMixin:
    """Exit pricing, exit orders and the sale they book."""
    def _maybe_exit_all(self) -> None:
        """
        Consider leaving each open position before the round decides it.

        MARKET and LIMIT only. BRACKET is not a flavour of these: its exit
        prices come from the price the position filled at, not from a model
        reading of the book, and its legs are placed by _arm_bracket at entry
        rather than reconsidered every pass. Running this over a bracketed
        position would stack a second, model-priced offer on shares that
        already have one.
        """
        if self._cfg.exit_order_type not in ("MARKET", "LIMIT"):
            return
        offered = {(p.rnd.topic_id, p.plan.side)
                   for p in self._pending.values()
                   if p.plan.action is Action.SELL}
        for pos in list(self._positions.values()):
            if (pos.rnd.topic_id, pos.signal.side) in offered:
                continue                  # already offered; do not stack
            self._post_exit(pos)

    def _model_prob(self, pos: Position) -> float | None:
        """
        The model's current probability for the side this position holds.

        Follows _maybe_scale_in exactly, including the clamped-sigma refusal:
        an overstated sigma inflates the tail probabilities, and pricing an
        EXIT off an inflated probability holds out for a price the model only
        believes because its volatility estimate is broken.
        """
        if pos.rnd.strike is None:
            return None
        symbol = self._client.market_symbol(pos.rnd.feed_symbol)
        secs = pos.rnd.seconds_remaining(self._client.now_ms())
        if secs <= 0:
            return None
        try:
            spot = self._market_data.spot(symbol)
            sigma = self._vol.sigma_annual(symbol)
        except (ApiError, requests.RequestException) as exc:
            LOG.debug("%s: cannot price an exit yet: %s", pos.rnd.slug, exc)
            return None
        if self._cfg.halt_on_clamped_sigma and self._vol.is_clamped(symbol):
            return None
        tail_df = self._vol.tail_df(symbol)
        p_up = digital_up_probability(spot, pos.rnd.strike, sigma, secs,
                                      tail_df)
        return p_up if pos.signal.side is Side.UP else 1.0 - p_up

    def _post_exit(self, pos: Position) -> bool:
        """
        Offer this position back to the market. True if an order went out.

        The bar is the sell reservation price: the market must overpay by the
        same edge the entry demanded. Selling for less than the position is
        worth to the model is not an exit, it is a loss taken voluntarily.
        """
        prob = self._model_prob(pos)
        if prob is None:
            return False
        target = sell_reservation_price(prob, self._cfg, pos.rnd.fee_bps)
        if target is None:
            return False

        if self._cfg.exit_trigger == "POLLED":
            bids = self._market_data.bids(pos.rnd, pos.signal.side)
            if not bids or bids[0][0] < target:
                return False
            # The bid is already there, so cross it rather than queue behind.
            target = bids[0][0]
        target = pos.rnd.round_price(target)
        if not 0.0 < target < 1.0:
            return False

        shares = pos.held_shares
        order_type = (OrderType.LIMIT if self._cfg.exit_order_type == "LIMIT"
                      else OrderType.MARKET)
        plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                         order_type=order_type, amount=shares,
                         price_limit=(target if order_type is OrderType.LIMIT
                                      else None))
        if self._live:
            try:
                quote = self._client.get_quote(pos.rnd, plan)
                order_id = self._client.place_order(pos.rnd, quote)
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("%s: exit rejected: %s", pos.rnd.slug, exc)
                return False
        else:
            order_id = self._paper_book.place(plan, pos.rnd)
        self._pending[str(order_id)] = PendingOrder(
            order_id=str(order_id), rnd=pos.rnd, plan=plan,
            signal=pos.signal, expires_at_ms=pos.rnd.end_ms,
            filled_usdt=0.0, filled_shares=0.0, trade_id=pos.trade_id)
        LOG.info("OFFER %s %s | %.4f shares at %.4f (entry %.4f)",
                 pos.rnd.slug, pos.signal.side.value, shares, target,
                 pos.signal.fill_price)
        return True

    def _book_sale(self, order_id: str, pending: PendingOrder,
                   state: OrderState) -> PendingOrder:
        """
        Reduce or close a position that has been sold back to the market.

        A sold position never reaches settled_outcome and is never redeemed:
        there is no winning token to claim, because the shares are gone. The
        journal row therefore closes from PROCEEDS, and settle_source records
        which kind of ending it was so the calibration buckets can exclude it.
        """
        new_usdt = state.filled_usdt - pending.filled_usdt
        if new_usdt <= EPS:
            return pending
        key = (pending.rnd.symbol, pending.plan.side)
        pos = self._positions.get(key)
        if pos is None:
            LOG.error("%s: a sale filled for %.4f USDT with no position on "
                      "record; the shares are gone and nothing tracked them",
                      pending.rnd.slug, new_usdt)
            return replace(pending, filled_usdt=state.filled_usdt,
                           filled_shares=state.filled_shares)
        price = state.price or pending.plan.price_limit or 0.0
        shrunk: dict = {}
        if pos.shares is not None:
            # Counted in shares when the fill reported them. Reckoned by
            # cost, a sale of every fee-net share still looks a few percent
            # short of the stake and leaves a phantom remainder open.
            sold = state.filled_shares - pending.filled_shares
            if sold > pos.shares + EPS:
                LOG.error("%s: sale of %.4f shares exceeds the %.4f held; "
                          "refusing to net it", pending.rnd.slug, sold,
                          pos.shares)
                return replace(pending, filled_usdt=state.filled_usdt,
                               filled_shares=state.filled_shares)
            left = max(pos.shares - sold, 0.0)
            sold_cost = pos.committed_usdt * min(
                1.0, sold / max(pos.shares, EPS))
            remaining = pos.committed_usdt - sold_cost if left > EPS else 0.0
            shrunk = {"shares": left}
        else:
            sold_cost = state.filled_shares * pos.signal.fill_price
            if sold_cost > pos.committed_usdt + EPS:
                # Impossible: more shares came back than the position ever
                # held. Netting it silently would report a profit made from
                # nothing.
                LOG.error("%s: sale of %.4f shares exceeds the %.4f USDT "
                          "held; refusing to net it", pending.rnd.slug,
                          state.filled_shares, pos.committed_usdt)
                return replace(pending, filled_usdt=state.filled_usdt,
                               filled_shares=state.filled_shares)
            remaining = pos.committed_usdt - sold_cost
        if remaining <= EPS:
            pnl = state.filled_usdt - pos.committed_usdt
            self._journal.resolve_sold(pos.trade_id, state.filled_usdt,
                                       price, order_id, pos.committed_usdt)
            self._positions.pop(key, None)
            self._brackets.pop(key, None)
            self._record_sale(key[0], pnl)
            LOG.info("SOLD %s %s | %.4f USDT at %.4f (entry %.4f) P&L %+.4f",
                     pending.rnd.slug, pending.plan.side.value,
                     state.filled_usdt, price, pos.signal.fill_price, pnl)
        else:
            self._positions[key] = replace(pos, committed_usdt=remaining,
                                           **shrunk)
            LOG.info("%s: sold %.4f of %.4f USDT at %.4f; %.4f left to settle",
                     pending.rnd.slug, sold_cost, pos.committed_usdt, price,
                     remaining)
        return replace(pending, filled_usdt=state.filled_usdt,
                       filled_shares=state.filled_shares)

    def _record_sale(self, symbol: str, pnl: float) -> None:
        """
        Report a position closed by SELLING to the risk manager.

        THE GAP THIS CLOSES
        -------------------
        Every other ending reports itself from _settle_one. A sold position
        never reaches _settle_one -- it leaves through _book_sale -- so
        before this existed a profile that closes by selling reported no
        results at all: daily_loss_limit_pct saw nothing, the consecutive
        loss counter never moved, and the breaker meant to stop a bad day was
        wired to a path such a profile never takes.

        That was survivable while selling was rare. It is not survivable for
        `scalp`, which closes every position this way, twenty times a round.

        model_prob is deliberately not passed. The calibration statistics
        answer one question -- did the price paid predict the outcome -- and
        a position closed BEFORE the outcome existed has no answer to
        contribute. Feeding one in would put a number in that bucket that no
        round ever produced, which is the single figure this bot exists to
        get right.

        The paper bankroll moves here for the same reason: in paper mode
        _settle_one is what credits P&L, so without this a paper scalper
        would trade all day against a balance that never changed.
        """
        if not self._live:
            self._paper_bankroll += pnl
        self._risk_for(symbol).record_result(pnl > 0, pnl=pnl)
        if self._account_risk is not None:
            self._account_risk.record_result(pnl > 0, pnl=pnl)

    @staticmethod
    def _sellable_shares(held: float) -> float:
        """
        The share count to offer for a holding recorded as `held`.

        Neither count the bot has is the exact holding. The quote's amountOut
        overstates it when the fill slips, and the order record's
        filledShareQty is rounded to two decimals -- sometimes up: a buy
        quoted 1.447444 shares recorded 1.45, and a sale of 1.45 was refused
        with -9000 "You have exceeded your available shares", then the
        flatten, and the stake settled as a full loss. So half a unit comes
        off before sizing down to the share precision. The remainder is dust
        and settles with the round. Decimal, because float arithmetic would
        turn 19.595 into 19.594999... and take a whole extra unit.
        """
        unit = Decimal(1).scaleb(-SHARE_PRECISION)
        sized = (Decimal(str(held)) - unit / 2).quantize(unit,
                                                         rounding=ROUND_DOWN)
        return float(sized) if sized > 0 else 0.0

    def _sell_now(self, pos: Position, reason: str) -> bool:
        """
        Offer the whole position back to the market, to be filled now.

        A MARKETABLE LIMIT, NOT A MARKET ORDER, and the difference matters at
        exactly the moment this is called. A MARKET order here is FOK: on the
        thin book that triggered the stop it fills entirely or not at all, and
        "not at all" is a position that was supposed to be capped and is now
        running to settlement. A limit priced through the bid sweeps whatever
        depth exists, keeps the partial fill, and rests the remainder where
        the reaper will retract it.

        The price is the best bid less the profile's own slippage cap, so how
        far this is willing to reach through the book is the number that
        already governs how far every other order may reach.

        True when an order went out. False means the position is still open
        and nothing is protecting it, which every caller reports loudly.
        """
        bids = self._market_data.bids(pos.rnd, pos.signal.side)
        if not bids:
            LOG.error("%s: nothing is bidding for %s; cannot sell",
                      pos.rnd.slug, pos.signal.side.value)
            return False
        floor = bids[0][0] * (1.0 - self._cfg.max_slippage_bps / 10_000.0)
        price = pos.rnd.round_price(floor)
        if not 0.0 < price < 1.0:
            LOG.error("%s: a sale through the bid prices at %.4f, which is "
                      "not tradable", pos.rnd.slug, price)
            return False
        shares = self._sellable_shares(pos.held_shares)
        if shares <= 0:
            return False
        unit = Decimal(1).scaleb(-SHARE_PRECISION)
        for attempt in (1, 2):
            plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                             order_type=OrderType.LIMIT, amount=shares,
                             price_limit=price)
            try:
                if self._live:
                    quote = self._client.get_quote(pos.rnd, plan)
                    order_id = str(self._client.place_order(pos.rnd, quote))
                else:
                    order_id = str(self._paper_book.place(plan, pos.rnd))
                break
            except (ApiError, requests.RequestException) as exc:
                LOG.error("%s: the %s sale of %.6f shares was refused "
                          "(recorded shares %s, cost-implied %.6f): %s",
                          pos.rnd.slug, reason, shares, pos.shares,
                          pos.committed_usdt / max(pos.signal.fill_price, EPS),
                          exc)
                # Only "more shares than are available" is worth asking
                # again, and only once: a holding can sit further under the
                # count than the margin allows, and a unit lower is the
                # difference between a capped exit and a full-stake loss.
                lower = float(Decimal(str(shares)) - unit)
                if (attempt == 2 or not isinstance(exc, ApiError)
                        or exc.code != -9000 or lower <= 0):
                    return False
                LOG.info("%s: retrying the %s sale at %.*f shares", pos.rnd.slug,
                         reason, SHARE_PRECISION, lower)
                shares = lower
        self._pending[order_id] = PendingOrder(
            order_id=order_id, rnd=pos.rnd, plan=plan, signal=pos.signal,
            expires_at_ms=pos.rnd.end_ms, filled_usdt=0.0, filled_shares=0.0,
            trade_id=pos.trade_id)
        LOG.info("SELL %s %s | %.4f shares through the bid at %.4f (%s)",
                 pos.rnd.slug, pos.signal.side.value, shares, price, reason)
        return True
