"""
Binance spot data. Model input only: the venue settles on its own
feed, so nothing here decides an outcome.
"""

from __future__ import annotations

import re

import requests

from btc5m.constants import LOG
from btc5m.errors import ApiError
from btc5m.venue.endpoints import BASE


class SpotApiMixin:
    """Spot price and klines, the model's view of the underlying."""
    def market_symbol(self, feed_symbol: str | None) -> str:
        """
        Map the venue's oracle symbol to a tradable Binance symbol.

        The market resolves on its own feed (e.g. Pyth "BTC/USD"), which is not
        a valid Binance ticker. Passing it through unchecked makes every price
        request fail. Normalise, verify once, and fall back to BTCUSDT with a
        loud warning -- the fallback carries basis risk, so it must be visible
        rather than silent.
        """
        if not feed_symbol:
            return self._cfg.symbol
        if feed_symbol in self._symbol_cache:
            return self._symbol_cache[feed_symbol]

        candidate = re.sub(r"[^A-Z0-9]", "", feed_symbol.upper())
        resolved = self._cfg.symbol
        if candidate:
            try:
                r = self._session.get(BASE + "/api/v3/ticker/price",
                                      params={"symbol": candidate},
                                      timeout=self._cfg.http_timeout_s)
                if r.status_code == 200:
                    resolved = candidate
            except requests.RequestException as exc:
                LOG.warning("Could not verify symbol %r (%s); falling back",
                            candidate, exc)

        if resolved != candidate:
            LOG.warning("Settlement feed %r is not a Binance symbol; modelling "
                        "on %s instead. Basis risk between the two feeds "
                        "is NOT captured by the model.", feed_symbol,
                        self._cfg.symbol)
        self._symbol_cache[feed_symbol] = resolved
        return resolved

    def spot_price(self, symbol: str | None = None) -> float:
        r = self._session.get(BASE + "/api/v3/ticker/price",
                              params={"symbol": symbol or self._cfg.symbol},
                              timeout=self._cfg.http_timeout_s)
        if r.status_code == 451:
            raise ApiError(
                "HTTP 451: Binance blocks this server's region. Host outside "
                "the United States (Frankfurt or Singapore on Render).",
                status=451)
        r.raise_for_status()
        return float(r.json()["price"])

    def kline_closes(self, symbol: str, limit: int) -> list[float]:
        """
        Recent 1m closes, most recent last.

        Lives here rather than in the volatility estimator because two
        callers need it now: the estimator, and the socket feed seeding its
        window. One fetch, one parse, one place to fix.
        """
        r = self._session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": limit},
            timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        return [float(k[4]) for k in r.json()]
