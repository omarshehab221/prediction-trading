"""The venue's addresses, verified against @binance/w3w-prediction."""

from __future__ import annotations

BASE = "https://api.binance.com"


# Verified against @binance/w3w-prediction. Overridable in the config
# file under "endpoints"; run --write-config to generate one.
# (HTTP method, path). The verb travels WITH the path: keeping them apart is
# what produced "Request method 'GET' is not supported" on trade/get-quote.
# Methods verified against @binance/w3w-prediction 2.0.1.
DEFAULT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "category_list": ("GET", "/sapi/v1/w3w/wallet/prediction/category/list"),
    "market_list": ("GET", "/sapi/v1/w3w/wallet/prediction/market/list"),
    "market_detail": ("GET", "/sapi/v1/w3w/wallet/prediction/market/detail"),
    "order_book": ("GET", "/sapi/v1/w3w/wallet/prediction/order-book"),
    "last_trade_price": ("GET", "/sapi/v1/w3w/wallet/prediction/order-book/last-trade-price"),
    "wallet_list": ("GET", "/sapi/v1/w3w/wallet/prediction/wallet/list"),
    "balances": ("GET", "/sapi/v1/w3w/wallet/prediction/balance/payment-options"),
    "quota_status": ("GET", "/sapi/v1/w3w/wallet/prediction/quota/limit/status"),
    "get_quote": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/get-quote"),
    "place_order": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/place-order-bundle"),
    "positions": ("GET", "/sapi/v1/w3w/wallet/prediction/position/list"),
    "settled_history": ("GET", "/sapi/v1/w3w/wallet/prediction/position/settled-history"),
    "order_history": ("GET", "/sapi/v1/w3w/wallet/prediction/order/history"),
    "order_list": ("GET", "/sapi/v1/w3w/wallet/prediction/order/list"),
    "batch_cancel": ("POST", "/sapi/v1/w3w/wallet/prediction/trade/batch-cancel"),
    "batch_redeem": ("POST", "/sapi/v1/w3w/wallet/prediction/batch-redeem"),
    "redeem_status": ("GET", "/sapi/v1/w3w/wallet/prediction/redeem/status"),
    "portfolio": ("GET", "/sapi/v1/w3w/wallet/prediction/pnl/portfolio"),
    # Transfers are handled inline by place-order's fundTransferAmount, so
    # the standalone transfer endpoints are deliberately not wired up.
}


# `accountType` on place-order accepts ONLY these. Any other value the venue
# lists as a payment option (the prediction wallet itself) is NOT valid there,
# and passing one through produces -3026 with no field named.
CEX_ACCOUNT_TYPES = ("SPOT", "FUNDING")
