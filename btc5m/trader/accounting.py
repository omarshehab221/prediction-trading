"""What the bot has, what it has committed, and what is left to stake."""

from __future__ import annotations

from dataclasses import replace

import requests

from btc5m.constants import EPS, LOG
from btc5m.errors import ApiError
from btc5m.risk import RiskManager
from btc5m.sizing import boosted_stake, kelly_stake

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Round, Signal


class AccountingMixin:
    """Bankroll, exposure, per-market risk and sizing against them."""
    def _risk_for(self, symbol: str) -> RiskManager:
        """Streak and calibration state for one market, created on demand."""
        if symbol not in self._risk:
            source = self._store or self._cfg
            self._risk[symbol] = RiskManager(source, self._bankroll())
        return self._risk[symbol]

    def _committed(self) -> float:
        return sum(p.committed_usdt for p in self._positions.values())

    def _outstanding(self) -> float:
        """
        The bot's own money in flight: staked, or won but not yet credited.

        Both halves have to be counted. Reconciliation compares the balance
        against cost-basis arithmetic, and either one of these makes those
        two disagree by an amount that is the bot's doing -- see
        RiskManager.reconcile for what mistaking it for an external flow
        did to the daily baseline.
        """
        with self._claim_lock:
            pending = sum(v for v, _, _ in self._unredeemed.values())
        return self._committed() + pending

    def _available(self, bankroll: float) -> float:
        """
        Bankroll that may back a NEW position.

        Sizing against the full balance while other markets already hold
        positions is how N concurrent trades quietly become N times the
        intended exposure. The reserve keeps some powder dry regardless.
        """
        return max(0.0, bankroll * (1.0 - self._cfg.reserve_pct)
                   - self._committed())

    def _bankroll(self) -> float:
        """
        Tradable balance, including winnings that are settled but not yet
        credited on chain. Excluding them would make a winning streak look
        like a drawdown and falsely trip the daily loss limit.
        """
        if not self._live:
            return self._paper_bankroll

        # The API reading is authoritative. Unclaimed winnings are added only
        # when explicitly configured, because the portfolio endpoint already
        # includes settled positions and counting them twice inflates the
        # bankroll by the gross payout.
        balance = self._client.balance_usdt()
        if not self._cfg.count_unredeemed_in_bankroll:
            return balance
        # Snapshot under the lock: this dict is now written from the claim
        # worker thread too, and iterating it live could race a mutation.
        with self._claim_lock:
            pending = sum(v for v, _, _ in self._unredeemed.values())
        return balance + pending

    def _live_bankroll(self, context: str) -> float | None:
        """Freshly read tradable balance, or None if it cannot be read."""
        try:
            return self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.warning("Could not confirm the balance before %s: %s",
                        context, exc)
            return None

    def _resize(self, sig: Signal, rnd: Round,
                bankroll: float) -> Signal | None:
        """Re-derive the stake against a balance that has since changed."""
        stake = kelly_stake(bankroll, sig.model_prob, sig.fill_price,
                            self._cfg, rnd.fee_bps)
        if stake <= 0:
            LOG.info("Balance fell to %.2f; no stake clears the limits now",
                     bankroll)
            return None
        if sig.trend_boosted:
            stake = boosted_stake(stake, bankroll, sig.model_prob,
                                  sig.fill_price, self._cfg, rnd.fee_bps)
        if self._cfg.scale_in:
            first = max(stake * self._cfg.scale_in_initial_pct,
                        self._cfg.min_stake_usdt)
            stake = min(first, stake)
        if abs(stake - sig.stake_usdt) > EPS:
            LOG.info("Resized %.2f -> %.2f against a live balance of %.2f",
                     sig.stake_usdt, stake, bankroll)
        return replace(sig, stake_usdt=stake)
