"""Which rounds exist and what their books look like."""

from __future__ import annotations

from dataclasses import replace

from btc5m.constants import LOG
from btc5m.errors import ApiError

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Round, Side


class MarketsApiMixin:
    """Round discovery, market detail and order books."""
    def list_rounds(self) -> list[Round]:
        payload = self._request("market_list", {
            "l1Category": "crypto", "l2Category": "up-down",
            "sortBy": "END_DATE", "orderBy": "ASC",
            "limit": self._cfg.market_list_limit})
        # One listing call covers every up/down market; _parse_round keeps
        # only the configured symbols, or every symbol when none are
        # configured -- that is how auto-discovery of new markets works.
        out = []
        for topic in payload.get("marketTopics") or []:
            rnd = self._parse_round(topic)
            if rnd is not None:
                out.append(rnd)
        return out

    def market_detail(self, topic_id: int) -> dict:
        return self._request("market_detail",
                             {"marketTopicId": topic_id})

    def hydrate(self, rnd: Round) -> Round | None:
        """
        Fill in strike and feed symbol from market/detail.

        The strike is variantData.startPrice. Reconstructing it from Binance
        klines would introduce basis risk, because the market resolves on its
        own price feed rather than on Binance spot.
        """
        if rnd.strike is not None:
            return rnd
        try:
            topic = self.market_detail(rnd.topic_id)
        except ApiError as exc:
            LOG.warning("Market detail unavailable for %s: %s", rnd.slug, exc)
            return None
        vd = (topic.get("marketTopic") or topic).get("variantData") or {}
        strike, symbol = self._parse_variant(vd)
        return None if strike is None else replace(rnd, strike=strike,
                                                   feed_symbol=symbol)

    def asks_for(self, rnd: Round, side: Side
                 ) -> list[tuple[float, float]] | None:
        """Ask ladder for one outcome. `vendor` is a required parameter."""
        try:
            payload = self._request("order_book", {
                "vendor": rnd.vendor, "marketId": rnd.market_id,
                "tokenId": rnd.token_for(side)})
        except ApiError as exc:
            LOG.debug("order book unavailable: %s", exc)
            return None
        return self._parse_asks(payload)

    def bids_for(self, rnd: Round, side: Side
                 ) -> list[tuple[float, float]] | None:
        """
        Bid ladder for one outcome -- the price a SALE would actually get.

        Pricing an exit off the ask reads the price someone is asking, not
        the price anyone is offering, and on a thin book those are not close.
        """
        try:
            payload = self._request("order_book", {
                "vendor": rnd.vendor, "marketId": rnd.market_id,
                "tokenId": rnd.token_for(side)})
        except ApiError as exc:
            LOG.debug("order book unavailable: %s", exc)
            return None
        return self._parse_levels(payload, "bids")
