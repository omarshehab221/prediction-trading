"""Whose money, where it sits, and how much of it may be spent."""

from __future__ import annotations

import math

from btc5m.constants import LOG
from btc5m.domain import WalletRef
from btc5m.errors import ApiError
from btc5m.venue.endpoints import CEX_ACCOUNT_TYPES


class AccountApiMixin:
    """Wallets, balances, funding source and the venue's quota."""
    def wallet(self) -> WalletRef:
        """Prediction wallet address + id. Cached; required by most calls."""
        if self._wallet is not None:
            return self._wallet
        payload = self._request("wallet_list")
        for w in payload.get("wallets") or []:
            addr, wid = w.get("walletAddress"), w.get("walletId")
            if addr and wid:
                self._wallet = WalletRef(str(addr), str(wid))
                return self._wallet
        raise ApiError("no prediction wallet found -- create one in the "
                       "Binance app and complete SAS authorization")

    def prediction_wallet_value(self) -> float | None:
        """
        Current value held inside the prediction wallet, from the portfolio.

        payment-options reports the CEX accounts; it does not necessarily
        include the prediction wallet, so a funded prediction account can read
        as 0.00 there. This is the second place to look.
        """
        try:
            payload = self._request("portfolio",
                                    {"walletAddress": self.wallet().address})
        except ApiError as exc:
            LOG.debug("Portfolio lookup failed: %s", exc)
            return None
        raw = payload.get("totalCurrentValue")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable totalCurrentValue %r", raw)
            return None
        return value if math.isfinite(value) else None

    def payment_options(self) -> list[tuple[str, float, bool]]:
        """
        Every funding option as (accountType, balance, enabled).

        Returned whole rather than filtered, because collateral can sit under
        an account type the caller did not anticipate. Silently filtering to
        one type reports 0.00 for a funded account -- which looks like an
        empty wallet rather than a lookup in the wrong place.
        """
        payload = self._request("balances")
        out: list[tuple[str, float, bool]] = []
        for item in payload.get("items") or []:
            try:
                bal = float(item.get("availableBalanceDisplay") or 0.0)
            except (TypeError, ValueError):
                bal = 0.0
            out.append((str(item.get("accountType") or "UNKNOWN").upper(),
                        bal, bool(item.get("enabled", True))))
        return out

    def funding_plan(self) -> tuple[str, str, str | None]:
        """
        Decide how an order gets paid for: (accountType, fundingSource, holder).

        Three places can hold collateral, and they are not interchangeable:

          * the prediction wallet itself (the MPC wallet) -- funds are already
            where the order needs them, so fundingSource=MPC and no transfer;
          * SPOT or FUNDING -- Binance exchange accounts, so fundingSource=CEX
            and the collateral must be moved in.

        `accountType` on place-order accepts ONLY SPOT or FUNDING. The
        prediction wallet is not a legal value there, so when it holds the
        funds we still nominate a CEX account for the parameter and let
        fundingSource=MPC say where the money really is. Passing the
        prediction account through verbatim is what produced -3026.

        `holder` is the account type actually holding the largest balance, or
        None when nothing is funded.
        """
        options = [(t, b) for t, b, en in self.payment_options() if en]
        holder = max(options, key=lambda kv: kv[1])[0] if options else None

        if self._cfg.funding_source != "AUTO":
            funding = self._cfg.funding_source
        elif holder is None or holder not in CEX_ACCOUNT_TYPES:
            funding = "MPC"        # already in the prediction wallet
        else:
            funding = "CEX"

        if self._cfg.account_type != "AUTO":
            account = self._cfg.account_type
        elif holder in CEX_ACCOUNT_TYPES:
            account = holder
        else:
            # Nominate a valid CEX account; fundingSource carries the truth.
            cex = [(t, b) for t, b in options if t in CEX_ACCOUNT_TYPES]
            account = max(cex, key=lambda kv: kv[1])[0] if cex else "SPOT"

        if account not in CEX_ACCOUNT_TYPES:
            raise ApiError(f"accountType {account!r} is not a valid payment "
                           f"account; must be one of {CEX_ACCOUNT_TYPES}")
        return account, funding, holder

    def resolved_funding_source(self) -> str:
        return self.funding_plan()[1]

    def balance_usdt(self) -> float:
        """
        Collateral available to trade, across every place it can sit.

        payment-options covers the CEX accounts only. A prediction wallet
        funded directly does not appear there, so reading that endpoint alone
        reports 0.00 for an account that plainly has money in it. Both sources
        are consulted and the larger is used.
        """
        options = self.payment_options()
        usable = [(t, b) for t, b, en in options if en]

        if self._cfg.account_type != "AUTO":
            for acct, bal in usable:
                if acct == self._cfg.account_type:
                    return bal
            raise ApiError(
                f"no enabled {self._cfg.account_type} option; available: "
                + ", ".join(f"{t}={b:.2f}" for t, b in usable) or "none")

        best = max((b for _, b in usable), default=0.0)
        in_wallet = self.prediction_wallet_value()
        if in_wallet is not None and in_wallet > best:
            LOG.debug("Using prediction wallet balance %.2f USDT", in_wallet)
            return in_wallet
        if not usable and in_wallet is None:
            raise ApiError("no funded account found: payment-options is empty "
                           "and the portfolio could not be read")
        return best

    def remaining_quota_usdt(self) -> float | None:
        """
        Venue-imposed daily trading limit, or None if the venue reports none.

        Errors propagate: swallowing them here made preflight print "OK None"
        for an endpoint that had actually failed, which is worse than no check.
        """
        payload = self._request("quota_status")
        raw = payload.get("remainingDailyLimit")
        return None if raw is None else float(raw)
