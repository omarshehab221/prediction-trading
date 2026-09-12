"""Paper orders, filled from the same book a live order would hit."""

from __future__ import annotations

import itertools

from btc5m.constants import EPS
from btc5m.domain import Action, OrderState

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import OrderPlan, Round

class PaperBook:
    """
    A simulated venue for resting orders, so paper mode tests the real thing.

    Paper mode used to be trivial because a MARKET FOK order either fills at
    the quoted price or does not exist. A GTC order has a life: it rests, it
    fills in pieces as depth appears, and it has to be cancelled. Simulating
    that as an instant full fill would make paper a market order wearing a
    different name, and it would report a fill rate the live bot can never
    reach -- which is worse than not testing it, because it looks like
    evidence.

    Fills are read off the same ladders the live path prices against: a BUY
    fills against asks at or below its limit, a SELL against bids at or above
    it, and only for the depth actually shown. No queue position is modelled;
    that would be a claim about the venue's matching engine that nothing here
    can check.

    order_state and cancel_orders are named exactly as PredictionClient's, so
    the reaper has one body and paper cannot drift into a shortcut through
    the lifecycle it exists to exercise.
    """

    def __init__(self, market_data) -> None:
        self._market_data = market_data
        self._orders: dict[str, tuple[OrderPlan, Round, bool]] = {}
        self._counter = itertools.count(1)

    def place(self, plan: OrderPlan, rnd: Round) -> str:
        order_id = f"paper-{next(self._counter)}"
        self._orders[order_id] = (plan, rnd, False)
        return order_id

    def cancel_orders(self, order_ids: list[str]
                      ) -> tuple[list[str], dict[str, str]]:
        cancelled = []
        for order_id in order_ids:
            entry = self._orders.get(order_id)
            if entry is None:
                continue
            self._orders[order_id] = (entry[0], entry[1], True)
            cancelled.append(order_id)
        return cancelled, {}

    def order_state(self, order_id: str) -> OrderState | None:
        entry = self._orders.get(order_id)
        if entry is None:
            return None
        plan, rnd, cancelled = entry
        shares, usdt, price = self._matched(plan, rnd)
        if cancelled:
            # Terminal, and still holding whatever filled before the cancel.
            return OrderState("DEAD", usdt, shares, price)
        if plan.action is Action.BUY:
            done = usdt >= plan.amount - EPS
        else:
            done = shares >= plan.amount - EPS
        if done:
            return OrderState("FILLED", usdt, shares, price)
        if usdt > 0:
            return OrderState("PARTIAL", usdt, shares, price)
        return OrderState("RESTING", 0.0, 0.0, None)

    def _matched(self, plan: OrderPlan,
                 rnd: Round) -> tuple[float, float, float | None]:
        """(shares, usdt, average price) this order would have taken by now."""
        limit = plan.price_limit
        if limit is None:
            return 0.0, 0.0, None
        if plan.action is Action.BUY:
            levels = self._market_data.asks(rnd, plan.side) or []
            crossing = [(pr, sz) for pr, sz in levels if pr <= limit + EPS]
            shares, spent = 0.0, 0.0
            for price, size in crossing:
                take = min(size, (plan.amount - spent) / price)
                if take <= 0:
                    break
                shares += take
                spent += take * price
            return shares, spent, (spent / shares if shares > 0 else None)
        levels = self._market_data.bids(rnd, plan.side) or []
        crossing = [(pr, sz) for pr, sz in levels if pr >= limit - EPS]
        shares, proceeds = 0.0, 0.0
        for price, size in crossing:
            take = min(size, plan.amount - shares)
            if take <= 0:
                break
            shares += take
            proceeds += take * price
        return shares, proceeds, (proceeds / shares if shares > 0 else None)
