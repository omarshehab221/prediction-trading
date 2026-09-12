"""How a round ended and how the winnings are claimed."""

from __future__ import annotations

import math

from btc5m.constants import LOG
from btc5m.domain import Side
from btc5m.errors import ApiError, NothingToRedeem

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Round


class SettlementApiMixin:
    """Settled outcomes, redemption and the settlement price."""
    # A redemption transaction that will never land. The claim worker has to
    # tell these apart from "not confirmed yet": one wants a fresh attempt,
    # the other wants more patience.
    DEAD_REDEEM_STATUSES = frozenset({
        "FAILED", "FAIL", "REVERTED", "DROPPED", "REJECTED", "ERROR",
        "CANCELLED", "CANCELED", "EXPIRED",
    })

    def settled_outcome(self, rnd: Round
                        ) -> tuple[Side, float | None] | None:
        """
        Authoritative result: (winning side, realised PnL in USDT).

        Read from the venue's settled position history rather than
        reconstructed locally, because the market resolves on its own oracle.
        Returns None until the position has actually settled.
        """
        try:
            payload = self._request("settled_history", {
                "walletAddress": self.wallet().address,
                "limit": self._cfg.settled_history_limit})
        except ApiError as exc:
            # Returning None silently would be indistinguishable from "this
            # round has not settled yet", so a broken lookup would look like
            # an open position rather than a failure.
            LOG.warning("Settled-history lookup failed for %s: %s",
                        rnd.slug, exc)
            return None

        for pos in payload.get("positions") or []:
            if int(pos.get("marketTopicId") or -1) != rnd.topic_id:
                continue
            if pos.get("isWinner") is None:
                continue
            name = str(pos.get("outcomeName", "")).upper()
            held = Side.UP if name in ("YES", "UP") else Side.DOWN
            winner = held if pos.get("isWinner") else (
                Side.DOWN if held is Side.UP else Side.UP)
            pnl_raw = pos.get("realizedPnl")
            if pnl_raw is None:
                pnl_raw = pos.get("pnl")
            if pnl_raw is None:
                LOG.warning("Settled position for %s reports no realised PnL",
                            rnd.slug)
                return winner, None
            try:
                return winner, float(pnl_raw)
            except (TypeError, ValueError):
                LOG.warning("Unparseable realised PnL %r for %s",
                            pnl_raw, rnd.slug)
                return winner, None
        return None

    def batch_redeem(self, token_ids: list[str], chain_id: str) -> list[str]:
        """
        Claim winnings. Returns transaction hashes to poll.

        Winning outcome tokens are NOT credited automatically -- redemption is
        an explicit on-chain action. Skipping it makes the tradable balance
        appear to fall after every win.
        """
        if not token_ids:
            raise ValueError("token_ids must not be empty")
        wallet = self.wallet()
        payload = self._request("batch_redeem", {
            "walletAddress": wallet.address,
            "walletId": wallet.wallet_id,
            "tokenIds": token_ids,
            "chainId": chain_id})

        hashes = []
        for res in payload.get("results") or []:
            # Only txHash exists in the schema. The `transactionHash` fallback
            # here was invented -- a field name that appears nowhere in the
            # connector, so it could never have matched and only served to
            # make the code look more tolerant than it was.
            tx = res.get("txHash")
            if tx:
                hashes.append(str(tx))
        if not hashes:
            if payload.get("batchId"):
                LOG.info("Redemption batch %s accepted, no tx hash yet",
                         payload["batchId"])
            else:
                # Accepted, no hash, no batch: the venue took the request and
                # had nothing to act on. On a winning position that means the
                # tokens are already gone -- redeemed elsewhere -- and the
                # caller must stop, not resubmit.
                raise NothingToRedeem(
                    f"nothing redeemable for {', '.join(token_ids)}")
        return hashes

    def redeem_status(self, tx_hash: str) -> str | None:
        """Status of a redemption transaction, or None if unknown."""
        try:
            payload = self._request("redeem_status", {
                "walletAddress": self.wallet().address, "txHash": tx_hash})
        except ApiError as exc:
            LOG.debug("Redeem-status lookup failed for %s: %s", tx_hash, exc)
            return None
        status = payload.get("status")
        return None if status is None else str(status).upper()

    def final_price(self, rnd: Round) -> float | None:
        """variantData.endPrice once the round has resolved."""
        try:
            topic = self.market_detail(rnd.topic_id)
        except ApiError as exc:
            LOG.warning("Final-price lookup failed for %s: %s", rnd.slug, exc)
            return None
        vd = (topic.get("marketTopic") or topic).get("variantData") or {}
        raw = vd.get("endPrice") if isinstance(vd, dict) else None
        if raw is None:
            return None                 # genuinely not resolved yet
        try:
            price = float(raw)
        except (TypeError, ValueError, OverflowError):
            LOG.warning("Unparseable endPrice %r for %s", raw, rnd.slug)
            return None
        if not math.isfinite(price):
            LOG.warning("Non-finite endPrice %r for %s", raw, rnd.slug)
            return None
        if price <= 0:
            LOG.warning("Implausible endPrice %r for %s", raw, rnd.slug)
            return None
        return price
