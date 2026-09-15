"""
Shadow mode: the live code path against the real venue, and no order sent.

Paper mode is a different program from live -- a notional bankroll, screen
prices instead of quotes, no fill records -- so a paper session could never
show what the live bot would do. ShadowClient is the live client with its
three writes replaced: placing an order, cancelling one, and redeeming. Every
read is the venue's own: the balance it starts from, the books, the buy
quotes (which are non-binding and place nothing), settlement.

What cannot be mimicked, and is not pretended to be: the venue's answer to an
order it never received. A buy fills at the venue's own quote and a sale fills
against the live bid, so a venue-side refusal such as "Failed to execute the
market order" does not happen here.
"""

from __future__ import annotations

import itertools
import threading
from decimal import ROUND_DOWN, Decimal

from btc5m.constants import EPS, LOG, SHARE_PRECISION
from btc5m.domain import Action, OrderState, OrderType, Quote
from btc5m.errors import ApiError, OrderNotFilled
from btc5m.venue.client import PredictionClient

# Everything the venue treats as an instruction rather than a question.
WRITE_ENDPOINTS = frozenset({"place_order", "batch_cancel", "batch_redeem"})


class ShadowWriteBlocked(RuntimeError):
    """A write reached the request layer in shadow mode. It was not sent."""


def _floor_shares(shares: float) -> float:
    """The venue holds shares to SHARE_PRECISION; never credit more."""
    unit = Decimal(1).scaleb(-SHARE_PRECISION)
    return float(Decimal(str(shares)).quantize(unit, rounding=ROUND_DOWN))


class ShadowClient(PredictionClient):
    """The live client, with orders, cancels and redemptions simulated."""

    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self._shadow_lock = threading.Lock()
        self._shadow_ids = itertools.count(1)
        self._quoted: dict[str, tuple] = {}         # quote_id -> (rnd, plan)
        self._simulated: dict[str, dict] = {}       # order_id -> record
        self._holdings: dict[str, float] = {}       # token_id -> shares
        self._cash = 0.0
        self._start_balance: float | None = None

    # -- the wall --------------------------------------------------------

    def _request(self, name: str, params: dict | None = None) -> dict:
        # The last line, under every override below: whatever path a write
        # takes, it stops here rather than at the venue.
        if name in WRITE_ENDPOINTS:
            raise ShadowWriteBlocked(f"shadow mode never sends {name}")
        return super()._request(name, params)

    # -- money -----------------------------------------------------------

    def balance_usdt(self) -> float:
        """The venue's balance when the session began, moved by shadow trades."""
        if self._start_balance is None:
            self._start_balance = super().balance_usdt()
            LOG.info("SHADOW balance starts from the venue's %.2f USDT",
                     self._start_balance)
        with self._shadow_lock:
            return self._start_balance + self._cash

    # -- quotes ----------------------------------------------------------

    def get_quote(self, rnd, plan) -> Quote:
        """
        A buy is quoted by the venue. A sale is quoted here.

        The real account holds none of the shadow's shares, so the venue
        would refuse every sell quote with -9000 and no stop would ever sell.
        The sale is checked against the shadow's own holding the way the
        venue checks the real one, and priced at its limit, which is how the
        venue prices a limit sale's amountOut.
        """
        quote = (self._sell_quote(rnd, plan) if plan.action is Action.SELL
                 else super().get_quote(rnd, plan))
        with self._shadow_lock:
            self._quoted[quote.quote_id] = (rnd, plan)
        return quote

    def _sell_quote(self, rnd, plan) -> Quote:
        token = rnd.token_for(plan.side)
        with self._shadow_lock:
            held = self._holdings.get(token, 0.0)
        if plan.amount > held + EPS:
            raise ApiError(
                "POST /sapi/v1/w3w/wallet/prediction/trade/get-quote: HTTP 400: "
                "You have exceeded your available shares (shadow holds "
                f"{held:.4f}). (code -9000)", code=-9000, status=400)
        bids = self.bids_for(rnd, plan.side) or []
        limit = plan.price_limit
        best = bids[0][0] if bids else (limit or 0.0)
        return Quote(
            quote_id=f"shadow-q-{next(self._shadow_ids)}",
            average_price=max(best, limit) if limit is not None else best,
            amount_out=plan.amount * (limit if limit is not None else best),
            price_impact=0.0, fee_usdt=0.0, action=plan.action,
            order_type=plan.order_type, price_limit=limit)

    # -- orders ----------------------------------------------------------

    def place_order(self, rnd, quote: Quote,
                    stake_usdt: float | None = None) -> str:
        with self._shadow_lock:
            known = self._quoted.pop(quote.quote_id, None)
        if known is None:
            raise ApiError(f"shadow: quote {quote.quote_id} was never issued")
        q_rnd, plan = known
        order_id = f"shadow-{next(self._shadow_ids)}"
        record = {"rnd": q_rnd, "plan": plan, "status": "OPEN",
                  "usdt": 0.0, "shares": 0.0, "gross": 0.0, "price": None}
        if quote.order_type is OrderType.MARKET and plan.action is Action.BUY:
            usdt = stake_usdt if stake_usdt is not None else plan.amount
            shares = _floor_shares(quote.amount_out)
            record.update(status="FILLED", usdt=usdt, shares=shares,
                          price=quote.average_price)
            with self._shadow_lock:
                self._cash -= usdt
                token = q_rnd.token_for(plan.side)
                self._holdings[token] = self._holdings.get(token, 0.0) + shares
            LOG.info("SHADOW %s %s buy: %.4f USDT for %.2f shares at %.4f "
                     "(not sent)", q_rnd.slug, plan.side.value, usdt, shares,
                     quote.average_price)
        else:
            LOG.info("SHADOW %s %s %s %s: %.4f at limit %s (not sent)",
                     q_rnd.slug, plan.side.value, plan.order_type.value,
                     plan.action.value, plan.amount, plan.price_limit)
        with self._shadow_lock:
            self._simulated[order_id] = record
        if record["status"] == "OPEN":
            self._advance(record)
        return order_id

    def _advance(self, record: dict) -> None:
        """Fill a resting order against the live book, as far as it reaches."""
        if record["status"] != "OPEN":
            return
        rnd, plan = record["rnd"], record["plan"]
        limit = plan.price_limit
        fee = rnd.fee_bps / 10_000.0
        token = rnd.token_for(plan.side)
        if plan.action is Action.SELL:
            want = plan.amount - record["shares"]
            got_shares = gross = 0.0
            for price, size in self.bids_for(rnd, plan.side) or []:
                if limit is not None and price < limit - EPS:
                    break
                take = min(size, want - got_shares)
                if take <= EPS:
                    break
                got_shares += take
                gross += take * price
            if got_shares <= EPS:
                return
            proceeds = gross * (1.0 - fee)
            with self._shadow_lock:
                record["shares"] += got_shares
                record["usdt"] += proceeds
                record["gross"] += gross
                record["price"] = record["gross"] / record["shares"]
                self._cash += proceeds
                self._holdings[token] = self._holdings.get(token, 0.0) - got_shares
                if record["shares"] >= plan.amount - EPS:
                    record["status"] = "FILLED"
        else:
            budget = plan.amount - record["usdt"]
            spent = got_shares = 0.0
            for price, size in self.asks_for(rnd, plan.side) or []:
                if limit is not None and price > limit + EPS:
                    break
                take = min(size, (budget - spent) / price)
                if take <= EPS:
                    break
                got_shares += take
                spent += take * price
            if spent <= EPS:
                return
            delivered = _floor_shares(got_shares * (1.0 - fee))
            with self._shadow_lock:
                record["usdt"] += spent
                record["shares"] += delivered
                record["gross"] += spent
                record["price"] = record["gross"] / max(record["shares"], EPS)
                self._cash -= spent
                self._holdings[token] = self._holdings.get(token, 0.0) + delivered
                if record["usdt"] >= plan.amount - EPS:
                    record["status"] = "FILLED"

    def order_state(self, order_id: str) -> OrderState | None:
        record = self._simulated.get(str(order_id))
        if record is None:
            return None
        self._advance(record)
        if record["status"] in ("FILLED", "DEAD"):
            status = record["status"]
        else:
            status = "PARTIAL" if record["usdt"] > EPS else "RESTING"
        return OrderState(status, record["usdt"], record["shares"],
                          record["price"])

    def confirm_fill(self, order_id: str, requested_usdt: float) -> float:
        record = self._simulated.get(str(order_id))
        if record is None:
            raise ApiError(f"shadow: no order {order_id} on record")
        self._advance(record)
        if record["status"] == "DEAD" or record["usdt"] <= EPS:
            raise OrderNotFilled(
                f"order {order_id} did not fill: status {record['status']}, "
                f"filled {record['usdt']} (shadow)")
        self._remember_delivered(order_id, {"filledShareQty": record["shares"],
                                            "price": record["price"]})
        return record["usdt"]

    def cancel_orders(self, order_ids: list[str]
                      ) -> tuple[list[str], dict[str, str]]:
        cancelled = []
        with self._shadow_lock:
            for order_id in order_ids:
                record = self._simulated.get(str(order_id))
                if record is None:
                    continue
                if record["status"] == "OPEN":
                    record["status"] = "DEAD"
                cancelled.append(order_id)
        return cancelled, {}

    def active_orders(self, market_id: int | None = None) -> list[dict]:
        return []

    # -- settlement ------------------------------------------------------

    def batch_redeem(self, token_ids: list[str], chain_id: str) -> list[str]:
        """Credit 1 USDT per winning share still held. Nothing is sent."""
        hashes = []
        for token in token_ids:
            with self._shadow_lock:
                shares = max(self._holdings.pop(token, 0.0), 0.0)
                self._cash += shares
            LOG.info("SHADOW redeem %s: %.2f USDT credited (not sent)",
                     token, shares)
            hashes.append(f"shadow-tx-{token}")
        return hashes

    def redeem_status(self, tx_hash: str) -> str | None:
        if str(tx_hash).startswith("shadow-tx-"):
            return "SUCCESS"
        return super().redeem_status(tx_hash)
