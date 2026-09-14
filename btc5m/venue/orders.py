"""Getting an order onto the book and finding out what happened to it."""

from __future__ import annotations

import time

from btc5m.constants import LOG
from btc5m.domain import Action, OrderState, Quote, _market_buy
from btc5m.errors import ApiError, ErrorKind, OrderNotFilled, _AboveBalance
from btc5m.units import _as_float_or_none, from_wei, to_wei

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import OrderPlan, Round, Side


class OrdersApiMixin:
    """Quotes, orders, fills, cancellation and order state."""
    # Statuses that mean the order is done and did NOT result in a position.
    DEAD_ORDER_STATUSES = frozenset({
        "CANCELLED", "CANCELED", "KILLED", "EXPIRED", "REJECTED", "FAILED",
        "TERMINATED",
    })

    FILLED_ORDER_STATUSES = frozenset({"FILLED", "COMPLETED", "SUCCESS"})

    def effective_slippage_bps(self, rnd: Round) -> int:
        """
        The tighter of our risk cap and the venue's published tolerance.

        The venue's value is a maximum it will accept, not a recommendation --
        the listed default of 1200 bps would let a thin book fill us 12% worse
        than quoted. Taking the minimum keeps our cap authoritative without
        inventing a number the venue would reject.
        """
        venue = rnd.venue_slippage_bps
        if venue <= 0:
            return self._cfg.max_slippage_bps
        return max(1, min(self._cfg.max_slippage_bps, venue))

    def _price_param(self, rnd: Round, price: float) -> str:
        """
        A limit price formatted for the wire.

        Plain decimal, snapped to the market's own precision. NOT wei:
        amountIn's doc comment says wei explicitly and priceLimit's says only
        "must be > 0", so passing it through to_wei sends a price of roughly
        1e18 on a market whose prices live in (0, 1).
        """
        return f"{rnd.round_price(price):.{rnd.decimal_precision}f}"

    def get_quote(self, rnd: Round, plan: OrderPlan) -> Quote:
        """
        Phase 1 of trading: ask the venue to price the trade.

        Returns the authoritative average fill price, so no local book-walking
        estimate is needed once we are live.

        Takes a plan rather than loose arguments because the order type, the
        side and the price have to agree, and only a value that carries all
        three can be checked for that.
        """
        params = {
            "walletAddress": self.wallet().address,
            "tokenId": rnd.token_for(plan.side),
            "side": plan.action.value,
            "amountIn": to_wei(plan.amount),
            "orderType": plan.order_type.value,
            "slippageBps": self.effective_slippage_bps(rnd),
            "chainId": rnd.chain_id,
            "feeRateBps": rnd.fee_bps,
            "fundingSource": self.resolved_funding_source()}
        if plan.price_limit is not None:
            params["priceLimit"] = self._price_param(rnd, plan.price_limit)
        payload = self._request("get_quote", params)

        quote_id = payload.get("quoteId")
        avg = payload.get("averagePrice")
        if not quote_id or avg is None:
            raise ApiError(f"malformed quote response: {payload}")

        avg_f = float(avg)
        if not 0.0 < avg_f < 1.0:
            raise ApiError(f"quote returned implausible price {avg_f}")

        out_raw = payload.get("amountOut")
        if out_raw is None:
            raise ApiError(f"quote omits amountOut: {payload}")
        amount_out = float(from_wei(out_raw))
        if amount_out <= 0:
            raise ApiError(f"quote returned {amount_out} out for "
                           f"{plan.amount:.2f} in")

        # Cross-check: the two amounts and the price must reconcile, or
        # averagePrice and amountOut describe different things and every
        # downstream calculation is wrong.
        #
        # The direction matters. A BUY puts USDT in and takes shares out, so
        # shares x price should equal the USDT. A SELL puts shares in and
        # takes USDT out, so the same product should equal the amount OUT.
        # Checking a sell the buy way rejects every well-formed sell quote.
        if plan.action is Action.BUY:
            implied, reference = amount_out * avg_f, plan.amount
        else:
            implied, reference = plan.amount * avg_f, amount_out
        tol = self._cfg.quote_consistency_tolerance
        if reference > 0 and abs(implied - reference) / reference > tol:
            raise ApiError(
                f"quote is internally inconsistent: {plan.action.value} of "
                f"{plan.amount:.4f} at {avg_f:.4f} implies {implied:.4f}, "
                f"not {reference:.4f}")

        impact_raw = payload.get("priceImpact")
        fee_raw = payload.get("feeAmount")
        return Quote(
            quote_id=str(quote_id),
            average_price=avg_f,
            amount_out=amount_out,
            # A missing impact is unknown, not zero; treat it as the worst
            # case so the caller's impact guard cannot be bypassed.
            price_impact=(float("inf") if impact_raw is None
                          else float(impact_raw)),
            fee_usdt=0.0 if fee_raw is None else float(from_wei(fee_raw)),
            action=plan.action,
            order_type=plan.order_type,
            price_limit=plan.price_limit)

    def discover_min_stake(self, rnd: Round, side: Side,
                           low: float = 0.25, high: float | None = None,
                           tolerance: float = 0.05) -> float | None:
        """
        Find the venue's actual minimum order size by probing get-quote.

        The connector documents "approximately 1.5 USDT (varies by market
        depth)" and publishes no field for it, so it cannot be read -- but it
        can be measured. Quotes are non-binding and place no order.

        `high` defaults to the account's own balance rather than an invented
        ceiling: probing above what the wallet holds returns "not enough
        USDT", which says nothing about the minimum and previously crashed
        the search. Errors are classified by the venue's numeric code, so a
        funds problem, an auth problem and a genuine size floor are never
        confused with one another.

        Returns None when no tested size quotes successfully.
        """
        if high is None:
            try:
                high = max(low * 2.0, self.balance_usdt())
            except ApiError as exc:
                LOG.warning("Could not read balance to bound the search: %s",
                            exc)
                high = low * 4.0
        if low <= 0 or high <= low:
            raise ValueError("require 0 < low < high")

        def quotable(amount: float) -> bool:
            """
            True if the venue quotes this size.

            Only a SIZE rejection counts as "too small". A funds error means
            the probe exceeded the balance and says nothing about the floor,
            so it bounds the search downward instead. Anything else is
            re-raised: treating an auth or parameter failure as "amount below
            minimum" turns a broken request into a confident wrong conclusion.
            """
            try:
                self.get_quote(rnd, _market_buy(side, amount))
                return True
            except ApiError as exc:
                if exc.kind is ErrorKind.SIZE:
                    LOG.debug("size %.2f rejected as too small: %s", amount, exc)
                    return False
                if exc.kind is ErrorKind.INSUFFICIENT_FUNDS:
                    LOG.debug("size %.2f exceeds the balance, not the floor",
                              amount)
                    raise _AboveBalance(amount) from exc
                raise                       # not a size problem -- surface it

        lo, hi = low, high
        try:
            if quotable(low):
                return low
            if not quotable(hi):
                return None
            while hi - lo > tolerance:
                mid = (lo + hi) / 2.0
                if quotable(mid):
                    hi = mid
                else:
                    lo = mid
            return hi
        except _AboveBalance as exc:
            # The probe ran past the wallet balance. Retry below it rather
            # than discarding everything learned so far.
            ceiling = exc.amount * 0.9
            if ceiling <= lo + tolerance:
                return None
            LOG.info("Re-bounding the search below the balance (%.2f)",
                     ceiling)
            return self.discover_min_stake(rnd, side, low, ceiling, tolerance)

    def place_order(self, rnd: Round, quote: Quote,
                    stake_usdt: float | None = None) -> str:
        """
        Phase 2: execute a quote.

        MARKET orders are FOK -- fill-or-kill, so there are no partial fills
        at prices the model never approved. LIMIT orders are GTC and REST:
        the id returned describes an order that may sit on the book for
        minutes, fill in pieces, or never fill at all. Confirming a limit
        order with confirm_fill would raise on the ordinary case; use
        order_state instead.

        Takes the round so chain and slippage come from the market rather
        than from a module-level assumption.

        Returns the venue order id. The response carries no fill price; the
        quote's averagePrice is the executed price.
        """
        wallet = self.wallet()
        account, funding, holder = self.funding_plan()
        params = {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "quoteId": quote.quote_id,
            # Derived from the quote's own order type, so the pairing the
            # venue enforces cannot be split across two call sites.
            "timeInForce": quote.order_type.time_in_force,
            "accountType": account,
            "orderType": quote.order_type.value,
            "slippageBps": self.effective_slippage_bps(rnd),
            "fundingSource": funding,
        }
        if quote.price_limit is not None:
            params["priceLimit"] = self._price_param(rnd, quote.price_limit)
        LOG.debug("Funding plan: accountType=%s fundingSource=%s holder=%s",
                  account, funding, holder)
        # A CEX-funded order needs the collateral moved to the prediction
        # wallet first; fundTransferAmount asks the venue to do it inline.
        if funding == "CEX" and self._cfg.auto_fund_transfer \
                and stake_usdt is not None:
            params["fundTransferAmount"] = to_wei(stake_usdt)

        payload = self._request("place_order", params)

        order_id = payload.get("orderId")
        if not order_id:
            raise ApiError(f"order not accepted: {payload}")
        return str(order_id)

    def order_fill(self, order_id: str) -> dict | None:
        """
        The venue's own record of an order: status, filled amount, price.

        Returns None when the order is not in the history yet, which is
        different from "it did not fill" and must not be conflated with it.
        """
        payload = self._request("order_history", {
            "walletAddress": self.wallet().address,
            "l1Category": "crypto",
            "limit": self._cfg.settled_history_limit})
        for order in payload.get("orders") or []:
            if str(order.get("orderId")) == str(order_id):
                return order
        return None

    def _remember_delivered(self, order_id: str, order: dict) -> None:
        """Keep the share count a confirmed fill's record reported."""
        # Created on first use: this mixin has no __init__ of its own.
        self.__dict__.setdefault("_delivered", {})[str(order_id)] = (
            _as_float_or_none(order.get("filledShareQty")))

    def delivered_shares(self, order_id: str) -> float | None:
        """
        The shares the venue delivered for an order confirm_fill confirmed.

        The quote's amountOut is an estimate the venue does not honour to
        the digit: it holds shares to two decimals, so a buy quoted at 2.294
        shares delivered 2.29, and every exit sized from the quote -- stop,
        take-profit and flatten alike -- was refused as exceeding the shares
        available. None when the record carried no count, or the order was
        never confirmed; the caller falls back to the quote. Handed over
        once, so a long run does not accumulate one entry per order.
        """
        return self.__dict__.get("_delivered", {}).pop(str(order_id), None)

    def confirm_fill(self, order_id: str, requested_usdt: float) -> float:
        """
        Confirm an order filled, returning the USDT actually filled.

        Polls because the history may lag the placement by a moment. Raises
        rather than returning zero when the order is dead or the fill is too
        small: a caller that receives a number can carry on with a position
        that does not exist, which is the failure this exists to prevent.
        """
        last_status = "unknown"
        for attempt in range(self._cfg.fill_confirm_attempts):
            order = self.order_fill(order_id)
            if order is not None:
                last_status = str(order.get("status") or "unknown").upper()
                filled = _as_float_or_none(order.get("filledUsdtAmount"))
                if filled is None:
                    filled = _as_float_or_none(order.get("filledShareQty"))
                if last_status in self.DEAD_ORDER_STATUSES:
                    # The venue has answered, and the answer is no. Callers
                    # must not record a position on this. The whole record
                    # travels with it: the status alone has never said why.
                    raise OrderNotFilled(
                        f"order {order_id} did not fill: status "
                        f"{last_status}, filled {filled}; venue record {order}")
                if last_status in self.FILLED_ORDER_STATUSES and filled:
                    # What the venue delivered, shares included. An exit sized
                    # from anything else has been refused as exceeding the
                    # shares available.
                    LOG.info("Order %s filled: venue record %s", order_id, order)
                    self._remember_delivered(order_id, order)
                    return filled
                if filled and filled >= requested_usdt * self._cfg.min_fill_fraction:
                    LOG.info("Order %s filled: venue record %s", order_id, order)
                    self._remember_delivered(order_id, order)
                    return filled
            if attempt + 1 < self._cfg.fill_confirm_attempts:
                time.sleep(self._cfg.fill_confirm_delay_s)

        raise ApiError(
            f"could not confirm order {order_id} filled after "
            f"{self._cfg.fill_confirm_attempts} attempts (last status "
            f"{last_status}); refusing to record a position that may not exist")

    def active_orders(self, market_id: int | None = None) -> list[dict]:
        """Orders the venue still considers live."""
        payload = self._request("order_list", {
            "walletAddress": self.wallet().address,
            "l1Category": "crypto",
            "marketId": market_id,
            "limit": self._cfg.settled_history_limit})
        orders = payload.get("orders")
        return list(orders) if isinstance(orders, list) else []

    def cancel_orders(self, order_ids: list[str]
                      ) -> tuple[list[str], dict[str, str]]:
        """
        Ask the venue to retract resting orders.

        Returns (cancelled ids, {id: reason} for the rest). The failures are
        reported and never interpreted here, because the usual reason a
        cancel fails is that the order filled first. A caller that reads
        `failed` as "still resting" walks away from a real position, which
        then settles, wins, and is never claimed because nothing knows it
        exists. The caller re-reads each order's state instead.
        """
        if not order_ids:
            return [], {}
        wallet = self.wallet()
        payload = self._request("batch_cancel", {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "cancelInfoList": [{"orderId": str(o)} for o in order_ids]})
        cancelled = [str(o) for o in (payload.get("canceled") or [])]
        failed: dict[str, str] = {}
        for entry in payload.get("failed") or []:
            if isinstance(entry, dict) and entry.get("orderId"):
                failed[str(entry["orderId"])] = str(entry.get("reason") or "")
        return cancelled, failed

    def order_state(self, order_id: str) -> OrderState | None:
        """
        Resting, partly filled, filled or dead -- an answer, not an exception.

        Active orders are consulted first: an order that is both live and
        partly filled appears there with its fill, and the history may not
        have caught up. Returns None when the venue has no record of the
        order at all, which is the absence of knowledge and must NOT be read
        as DEAD -- an order the history lags is still out there.
        """
        for order in self.active_orders():
            if str(order.get("orderId")) == str(order_id):
                return self._read_order(order)
        found = self.order_fill(order_id)
        if found is None:
            return None
        return self._read_order(found)

    def _read_order(self, order: dict) -> OrderState:
        """One venue order dict, read into an OrderState."""
        status = str(order.get("status") or "").upper()
        filled_usdt = _as_float_or_none(order.get("filledUsdtAmount")) or 0.0
        filled_shares = _as_float_or_none(order.get("filledShareQty")) or 0.0
        price = _as_float_or_none(order.get("price"))
        if status in self.DEAD_ORDER_STATUSES:
            state = "DEAD"
        elif status in self.FILLED_ORDER_STATUSES:
            state = "FILLED"
        elif filled_usdt > 0:
            state = "PARTIAL"
        else:
            state = "RESTING"
        return OrderState(state, filled_usdt, filled_shares, price)
