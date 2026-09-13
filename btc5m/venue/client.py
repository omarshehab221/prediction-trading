"""
The venue client: construction, request signing, transport, and the
parse-once rules for payloads that cannot be trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import time
import urllib.parse

import requests

from btc5m.config import Config
from btc5m.constants import DEFAULT_ROUND_SECONDS, LOG
from btc5m.domain import Round
from btc5m.errors import ApiError
from btc5m.venue.account import AccountApiMixin
from btc5m.venue.endpoints import BASE
from btc5m.venue.markets import MarketsApiMixin
from btc5m.venue.orders import OrdersApiMixin
from btc5m.venue.settlement import SettlementApiMixin
from btc5m.venue.spot import SpotApiMixin

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config_file import ConfigStore
    from btc5m.domain import WalletRef

class PredictionClient(
        SpotApiMixin,
        AccountApiMixin,
        MarketsApiMixin,
        OrdersApiMixin,
        SettlementApiMixin):
    """All venue I/O. Signs requests; parses payloads exactly once."""

    # Set from Config at construction; _parse_round is a staticmethod and
    # needs the target duration without a config reference.
    round_target_ms: int = DEFAULT_ROUND_SECONDS * 1000
    open_statuses: tuple[str, ...] = ("REGISTERED", "OPEN", "ACTIVE")
    tradable_status: str = "OPEN"
    duration_tolerance: float = 0.10
    # Empty means no restriction -- see Config.symbols.
    symbols: tuple[str, ...] = ()

    def __init__(self, cfg: Config | ConfigStore) -> None:
        # Accepts either a Config or a ConfigStore. With a store, `_cfg`
        # resolves to the current configuration on every access, so a hot
        # reload takes effect without rebuilding the client or its session.
        self._store: ConfigStore | None = None
        self._static_cfg: Config | None = None
        if isinstance(cfg, Config):
            self._static_cfg = cfg
        else:
            self._store = cfg
        cfg = self._cfg
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": cfg.api_key})
        self._clock_offset_ms = 0
        self._wallet: WalletRef | None = None
        self._symbol_cache: dict[str, str] = {}
        self.apply_config(cfg)
    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @staticmethod
    def apply_config(cfg: Config) -> None:
        """
        Push settings that _parse_round reads as class attributes.

        _parse_round is a staticmethod (it validates untrusted payloads with
        no instance to hand), so these must be refreshed whenever the config
        changes -- otherwise a hot reload would update everything except
        parsing, and the two would silently disagree.
        """
        PredictionClient.round_target_ms = cfg.round_seconds * 1000
        PredictionClient.open_statuses = cfg.open_statuses
        PredictionClient.tradable_status = cfg.tradable_status
        PredictionClient.duration_tolerance = cfg.round_duration_tolerance
        PredictionClient.symbols = tuple(cfg.symbols)

    @property
    def session(self) -> requests.Session:
        return self._session

    # -- time ---------------------------------------------------------------

    def sync_clock(self) -> int:
        r = self._session.get(BASE + "/api/v3/time",
                              timeout=self._cfg.http_timeout_s)
        r.raise_for_status()
        self._clock_offset_ms = (int(r.json()["serverTime"])
                                 - int(time.time() * 1000))
        if abs(self._clock_offset_ms) > 1000:
            LOG.warning("Local clock off by %d ms; compensating",
                        self._clock_offset_ms)
        return self._clock_offset_ms

    def now_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    # -- transport ----------------------------------------------------------

    def _signed_query(self, params: dict) -> str:
        """
        Build the exact query string to send, with its signature appended.

        Binance recomputes the HMAC over the query string it RECEIVES, so the
        signed bytes and the sent bytes must be byte-identical. Signing a
        sorted dict and then letting the HTTP client re-serialise it in
        insertion order produces a different string and a guaranteed -1022
        signature error. Returning a string rather than a dict makes that
        class of bug unrepresentable.

        Every array and object parameter is a JSON string, which is what the
        connector does and therefore what the venue expects. See below.
        """
        p = {k: v for k, v in params.items() if v is not None}
        # Arrays and objects go as JSON, because that is what the venue is
        # given by its own connector. @binance/common routes every parameter
        # through serializeValue, which JSON.stringify's anything that is not
        # a scalar, and then HMACs the string it built -- so the JSON form is
        # both what arrives and what the signature is checked against.
        #
        # This used to send flat lists as repeated parameters
        # (tokenIds=a&tokenIds=b) via urlencode's doseq. That was a guess
        # carried from the first commit which no live call ever tested:
        # batch_redeem cannot run until a real position exists, and
        # place-order-bundle has never executed against a funded account, so
        # the only two array parameters in the bot -- tokenIds and
        # cancelInfoList -- had never left the machine. Repeated parameters
        # are not a Binance array convention anywhere.
        #
        # doseq is therefore gone rather than left set: no list or tuple can
        # reach urlencode any more, so a doseq=True here would describe a
        # path that no longer exists.
        p = {k: (json.dumps(v, separators=(",", ":"))
                 if isinstance(v, (dict, list, tuple)) else v)
             for k, v in p.items()}
        p["timestamp"] = self.now_ms()
        p["recvWindow"] = self._cfg.recv_window_ms
        # Brackets stay literal in KEYS: the indexed form of a list,
        # cancelInfoList[0].orderId, failed -1022 when they went out as
        # %5B/%5D, because the venue checks the signature against the
        # brackets. Values are encoded exactly as urlencode encodes them.
        query = "&".join(
            f"{urllib.parse.quote(str(k), safe='[]')}="
            f"{urllib.parse.quote_plus(str(v))}"
            for k, v in sorted(p.items()))
        signature = hmac.new(self._cfg.api_secret.encode(),
                             query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"
    
    # Binance error codes worth explaining rather than echoing verbatim.
    _ERROR_HINTS = {  # noqa: RUF012
        -1022: "signature mismatch -- the signed and sent query strings differ",
        -1021: "timestamp outside recvWindow -- clock drift",
        -1102: "a mandatory parameter was missing or malformed",
        -2014: "API-key format invalid",
        -2015: "invalid API key, IP not whitelisted, or missing permissions",
        -1002: "not authorised for this endpoint",
        -9000: "the account balance cannot cover this order size",
        -3026: ("a parameter combination the venue rejected -- most often "
                "fundingSource not matching accountType (SPOT/FUNDING are "
                "CEX accounts, not MPC), or a missing fundTransferAmount"),
    }

    @staticmethod
    def _json_or_none(response) -> object | None:
        """Parsed JSON, or None when the body is not JSON at all."""
        try:
            return response.json()
        except ValueError:
            return None

    def _request(self, name: str, params: dict | None = None) -> dict:
        """Call a named endpoint. The verb comes from the table, never a caller."""
        method, path = self._cfg.ep(name)
        query = self._signed_query(params or {})
        if LOG.isEnabledFor(logging.DEBUG):
            # Signature redacted; every other parameter shown verbatim so a
            # malformed request is visible rather than inferred.
            LOG.debug("%s %s?%s", method, path,
                      re.sub(r"signature=[0-9a-f]+", "signature=<redacted>",
                             query))
        url = f"{BASE}{path}?{query}"
        try:
            r = self._session.request(method, url,
                                      timeout=self._cfg.http_timeout_s)
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path}: {exc}") from exc

        if r.status_code >= 400:
            # Binance puts the real diagnosis in the body, not the status line.
            # Discarding it turns every distinct failure into "400 Bad Request".
            code, body = None, self._json_or_none(r)
            if isinstance(body, dict) and "msg" in body:
                code = body.get("code")
                detail = f"{body['msg']} (code {code})"
            else:
                # Not a JSON error envelope; the raw text is the best detail
                # available and is preserved rather than discarded.
                detail = r.text[:200] or "<empty response body>"
            code_int: int | None = None
            if code is not None:
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = None
            hint = self._ERROR_HINTS.get(code_int)
            raise ApiError(f"{method} {path}: HTTP {r.status_code}: {detail}"
                           + (f" -- {hint}" if hint else ""),
                           code=code_int, status=r.status_code)

        try:
            payload = r.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path}: bad JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ApiError(f"{method} {path}: expected a JSON object")
        return payload

    @staticmethod
    def _parse_variant(vd: dict) -> tuple[float | None, str | None]:
        """(startPrice, priceFeedSymbol) from a variantData block."""
        if not isinstance(vd, dict):
            return None, None
        symbol = vd.get("priceFeedSymbol")
        raw = vd.get("startPrice")
        if raw is None:
            return None, symbol         # not published yet: expected early
        try:
            price = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable startPrice %r", raw)
            return None, symbol
        if not math.isfinite(price):
            LOG.warning("Non-finite startPrice %r", raw)
            return None, symbol
        if price <= 0:
            LOG.warning("Implausible startPrice %r", raw)
            return None, symbol
        return price, symbol

    @staticmethod
    def _parse_round(topic: dict) -> Round | None:
        """
        Validate untrusted payload once, into a precise type.

        Returns None for anything that is not a live 5-minute up/down market
        in a configured symbol (every symbol, when none are configured).
        Downstream code may assume every Round is well-formed. `strike` is left
        None here: the list response often omits variantData, and requiring it
        would reject every round and leave the bot silently never trading.
        """
        try:
            if topic.get("chartType") != "CRYPTO_UP_DOWN":
                return None
            # Empty PredictionClient.symbols means no restriction: every
            # symbol the venue lists is a candidate market.
            if (PredictionClient.symbols
                    and topic.get("symbol") not in PredictionClient.symbols):
                return None
            if topic.get("status") not in PredictionClient.open_statuses:
                return None

            start_ms, end_ms = int(topic["startDate"]), int(topic["endDate"])
            target = PredictionClient.round_target_ms
            if abs((end_ms - start_ms) - target) > target * PredictionClient.duration_tolerance:
                return None

            markets = topic.get("markets") or []
            if not markets:
                return None
            market = markets[0]
            if market.get("tradingStatus") != PredictionClient.tradable_status:
                return None

            outcomes = {str(o.get("name", "")).upper(): o
                        for o in market.get("outcomes") or []}
            up = outcomes.get("YES") or outcomes.get("UP")
            down = outcomes.get("NO") or outcomes.get("DOWN")
            if not up or not down:
                return None

            up_q, down_q = float(up["price"]), float(down["price"])
            if not (math.isfinite(up_q) and math.isfinite(down_q)):
                return None
            if not (0.0 < up_q < 1.0 and 0.0 < down_q < 1.0):
                return None

            vendor = topic.get("vendor")
            chain_id = topic.get("chainId")
            collateral = topic.get("collateral")
            if not vendor or not chain_id or not collateral:
                # Required for order routing. Guessing them would send a
                # correctly-formed order to the wrong place.
                LOG.warning("Market %s missing vendor/chainId/collateral",
                            topic.get("slug"))
                return None

            # Distinct names on purpose: `market_symbol` is the venue's ticker
            # for the contract (BTCUSDT), while `feed_symbol` is the oracle it
            # settles against. Sharing one name built the Round with the
            # ORACLE symbol -- or the string "None" when no feed was published
            # -- so positions, per-market risk and the journal were all keyed
            # on the wrong value.
            market_symbol = str(topic.get("symbol") or "")
            if not market_symbol:
                LOG.warning("Market topic has no symbol; skipping")
                return None
            strike, feed_symbol = PredictionClient._parse_variant(
                topic.get("variantData") or {})

            fee_raw = topic.get("feeRateBps")
            if fee_raw is None:
                LOG.warning("Market %s publishes no feeRateBps", topic.get("slug"))
                return None
            fee_bps = int(fee_raw)
            if not 0 <= fee_bps < 10_000:
                # A negative rate would inflate net odds and therefore stake
                # size; a rate at or above 100% makes the contract unpayable.
                # Neither is a market we should trade.
                LOG.warning("Market %s publishes implausible feeRateBps %s",
                            topic.get("slug"), fee_raw)
                return None

            prec_raw = market.get("decimalPrecision")
            if prec_raw is None:
                # Defaulting to 4 would round prices to a precision the venue
                # does not use, producing orders it may reject.
                LOG.warning("Market %s publishes no decimalPrecision",
                            topic.get("slug"))
                return None

            liq_raw = market.get("liquidity")
            if liq_raw is None:
                liq_raw = topic.get("liquidity")
            try:
                liquidity = None if liq_raw is None else float(liq_raw)
            except (TypeError, ValueError, OverflowError):
                liquidity = None
            if liquidity is not None and not math.isfinite(liquidity):
                liquidity = None

            return Round(
                topic_id=int(topic["marketTopicId"]),
                market_id=int(market["marketId"]),
                vendor=str(vendor),
                slug=str(topic.get("slug", "")),
                symbol=market_symbol,
                start_ms=start_ms, end_ms=end_ms,
                up_token_id=str(up["tokenId"]),
                down_token_id=str(down["tokenId"]),
                up_quote=up_q, down_quote=down_q,
                fee_bps=fee_bps,
                chain_id=str(chain_id),
                collateral=str(collateral).upper(),
                venue_slippage_bps=int(topic.get("slippageBps") or 0),
                decimal_precision=int(prec_raw),
                liquidity=liquidity,
                strike=strike, feed_symbol=feed_symbol)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # OverflowError is NOT a ValueError: int(float("inf")) raises it,
            # and "1e999" parses to inf before reaching int().
            LOG.warning("Skipping malformed market payload: %s", exc)
            return None

    @staticmethod
    def _parse_levels(payload: dict,
                      key: str) -> list[tuple[float, float]] | None:
        """
        One side of the book. Levels are {price, size} strings per the schema.

        Takes the key rather than assuming "asks", because selling prices off
        bids and the two sides are parsed by identical rules -- a second copy
        of this would be a second place for the skipped-level accounting to
        drift.
        """
        raw = payload.get(key)
        if raw is None:
            nested = payload.get("orderBook") or payload.get("data") or {}
            raw = nested.get(key) if isinstance(nested, dict) else None
        if not raw:
            return None
        if not isinstance(raw, (list, tuple)):
            # A scalar here is malformed; iterating it raises TypeError.
            LOG.warning("Order book %r is %s, not a list",
                        key, type(raw).__name__)
            return None

        levels: list[tuple[float, float]] = []
        skipped = 0
        for lvl in raw:
            try:
                if isinstance(lvl, dict):
                    price = float(lvl["price"])
                    size_raw = lvl.get("size")
                    if size_raw is None:
                        size_raw = lvl.get("quantity")
                    if size_raw is None:
                        size_raw = lvl.get("amount")
                    size = float(size_raw)
                else:
                    price, size = float(lvl[0]), float(lvl[1])
            except (TypeError, ValueError, KeyError, IndexError,
                    OverflowError):
                skipped += 1
                continue
            if not (math.isfinite(price) and math.isfinite(size)):
                skipped += 1
                continue
            if 0.0 < price < 1.0 and size > 0:
                levels.append((price, size))
            else:
                skipped += 1
        if skipped:
            # Silently dropping levels would understate depth and make the
            # book look thinner than it is.
            LOG.warning("Order book %r: skipped %d unparseable level(s) "
                        "of %d", key, skipped, len(raw))
        ordered = sorted(levels)
        # Asks read cheapest-first and bids read dearest-first, so [0] is the
        # touch on either side and no caller has to remember which is which.
        if key == "bids":
            ordered.reverse()
        return ordered or None

    @staticmethod
    def _parse_asks(payload: dict) -> list[tuple[float, float]] | None:
        """Ask ladder. Kept by name: TestParseAsks and fuzz.py both call it."""
        return PredictionClient._parse_levels(payload, "asks")
