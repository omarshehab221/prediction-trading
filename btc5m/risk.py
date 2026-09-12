"""
What stops the bot trading: losing streaks, the daily loss limit, and
a model whose calibration has drifted.
"""

from __future__ import annotations

import math
import threading
import time

from btc5m.config import Config
from btc5m.constants import EPS, LOG
from btc5m.errors import TradingHalted

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config_file import ConfigStore

class RiskManager:
    """Owns every reason to stop. Fails closed on all of them."""

    def __init__(self, cfg: Config | ConfigStore,
                 starting_bankroll: float) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._day_start_bankroll = max(starting_bankroll, EPS)
        self._day_key = time.strftime("%Y-%m-%d")
        self.consecutive_losses = 0
        self.rounds_today = 0
        self.halted_reason: str | None = None
        # Poisson-binomial accumulators: each trade contributes its own model
        # probability, so expectation is well defined even though every trade
        # has different odds.
        self._expected_wins = 0.0
        self._variance = 0.0
        self._actual_wins = 0
        self._samples = 0
        # Attribution. The daily loss limit used to be measured as a fall in
        # the raw venue balance, which silently counted every deposit,
        # withdrawal, transfer and manually placed order as the bot's own
        # trading result. On a small account that is not a rounding error: a
        # 5 USDT manual order against a 9 USDT balance reads as a 55%
        # drawdown and halts a bot that has not lost anything.
        #
        # So the limit is measured against realised PnL the bot can actually
        # account for, and any balance movement it cannot account for is
        # recorded as an external flow and rebased away rather than blamed on
        # the strategy.
        self._realised_pnl = 0.0
        self._external_flow = 0.0
        # The third thing a balance movement can be, and the one this used to
        # have no name for: the bot's OWN arithmetic being wrong. settle_pnl
        # models the fee and the fill, and an error in that model leaves
        # exactly the residue a deposit leaves. Every one of them was rebased
        # into the baseline and relabelled an external flow -- so a fee the
        # bot under-modelled by a few cents a round was laundered, round after
        # round, and the daily limit slowly stopped describing the account.
        # The venue is authoritative; corrections are booked as PnL and
        # counted here so the two can still be told apart in a report.
        self._pnl_correction = 0.0
        # A payout that has settled but not yet landed on chain. Recorded so
        # the drift it eventually causes can be charged to the trade rather
        # than to a phantom depositor -- and BOUNDED by it, so a withdrawal
        # sharing the same window is not swallowed whole as a trading loss.
        self._expected_credit = 0.0
        # Touched from the claim worker as well as the trading loop.
        self._lock = threading.Lock()

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @property
    def realised_pnl(self) -> float:
        """PnL from positions this bot opened and settled today."""
        return self._realised_pnl

    @property
    def external_flow(self) -> float:
        """Balance movement today that this bot did not cause."""
        return self._external_flow

    @property
    def pnl_correction(self) -> float:
        """How much the venue has moved today's PnL away from our own sums."""
        return self._pnl_correction

    def expect_credit(self, amount: float) -> None:
        """
        Declare a payout that has settled but has not landed yet.

        This is what licenses the next reconciliation to read a shortfall as
        a trading result. Without it a claim that credits light is
        indistinguishable from a withdrawal, and the bot has to assume the
        more flattering of the two.
        """
        with self._lock:
            self._expected_credit += max(amount, 0.0)

    def calibration_z(self) -> float | None:
        """
        How far observed wins sit below what the model predicted, in sigmas.

        None until there are enough samples. This is the honest health check
        for a low-win-rate strategy: losing 12 in a row on 10%-probability
        contracts is expected, whereas losing 12 in a row on 60% contracts
        means the model is broken. A streak counter cannot tell them apart.
        """
        if self._samples < self._cfg.calibration_min_samples:
            return None
        if self._variance <= EPS:
            return None
        return (self._actual_wins - self._expected_wins) / math.sqrt(self._variance)

    def _roll_day(self, bankroll: float) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._day_key:
            self._day_key = today
            self._day_start_bankroll = max(bankroll, EPS)
            self.consecutive_losses = 0
            self.rounds_today = 0
            self.halted_reason = None
            self._expected_wins = self._variance = 0.0
            self._actual_wins = self._samples = 0
            self._realised_pnl = 0.0
            self._external_flow = 0.0
            self._pnl_correction = 0.0
            self._expected_credit = 0.0
            LOG.info("New trading day; baseline bankroll %.2f", bankroll)

    def reconcile(self, bankroll: float,
                  outstanding: float | None = None) -> float:
        """
        Separate what this bot did to the balance from what anything else did.

        With nothing in flight the balance is fully predictable: the day's
        opening figure plus everything the bot has settled. Whatever is left
        over came from somewhere else -- a deposit, a withdrawal, a transfer
        between wallets, or an order placed by hand -- and is none of the
        strategy's doing. That residue is folded into the day's baseline
        instead of being counted as a result, and returned so the caller can
        report it.

        WHY THIS ONLY RUNS WHEN FLAT
        ----------------------------
        `bankroll` and the expectation above are measured on different bases
        the moment anything is outstanding, and the difference is the bot's
        OWN money -- which is exactly what this must not mistake for someone
        else's:

          * an open position. A wallet-funded account reads its balance from
            the portfolio's totalCurrentValue, which marks open positions to
            MARKET, while the arithmetic here knows them at COST. Every tick
            of the underlying then looked like a deposit or a withdrawal: a
            4.00 leg bought at 0.25 rebased the baseline by +4.00 on the spot
            and by another fraction of a USDT on every poll afterwards, with
            no trade taking place at all. Paper mode had the same symptom for
            a different reason -- _paper_bankroll is not debited when a stake
            goes out, so an open position read as a deposit of its own size.

          * a claim in flight. A win is booked into realised PnL the instant
            it settles, but the USDT lands on chain up to claim_timeout_s
            later. In between, the balance is short by the payout and the
            gap read as a withdrawal; when the redemption confirmed, the same
            payout arrived and read as a deposit. Every won round logged two
            phantom movements that cancelled out only by luck.

        So reconciliation waits for a flat book. Nothing is lost by waiting:
        drift is measured against the day's baseline plus cumulative realised
        PnL, so a genuine deposit made mid-round is still caught in full the
        first moment the bot is flat. `outstanding` is None for callers that
        do not own the balance at all -- the per-market managers share one
        account and must never rebase it.

        The tolerance is derived from the venue minimum rather than being a
        new tunable: anything smaller than a fraction of the smallest order
        the venue accepts cannot be a trade.

        WHAT A CLAIM IN FLIGHT CHANGES
        ------------------------------
        Not every residue is somebody else's money. A payout that settled and
        has not landed yet is the bot's own, and when it lands SHORT -- a fee
        the model missed, a partial redemption -- the difference is a trading
        result. Charging it to a phantom depositor is how a wrong fee model
        stays invisible: rebased away once per won round, for as long as the
        bot runs, while the daily limit drifts further from the account it is
        supposed to be protecting.

        So a declared credit (expect_credit) licenses a correction, and
        reconcile_tolerance bounds it: the venue may differ from our sums by
        that fraction of the payout, which is what an unmodelled fee or a
        partial redemption looks like. That is the same meaning the knob
        already carries at settlement, applied to the same question.

        The bound is the point. Drift beyond what a fee could explain is not
        a fee, so a withdrawal landing in the same window as a claim still
        reads as a withdrawal for all but a sliver -- which is what keeps a
        manual order from reading as a drawdown. The split of a genuinely
        ambiguous residue cannot be recovered; it can only be capped, and
        capping it in the strategy's DISFAVOUR is the safe direction.

        NOTHING IS EVER LEFT OVER
        -------------------------
        However the residue is split, ALL of it is absorbed, so that after
        this returns

            _day_start_bankroll + _realised_pnl == bankroll

        exactly. The venue's number is the truth and this pair is only a
        decomposition of it; a decomposition that does not add up is just a
        slower way of being wrong.

        This used to return early whenever the drift was under `tolerance`,
        on the reasoning that something smaller than a fraction of the venue
        minimum cannot be a trade. True, and irrelevant: the drift is
        measured against the baseline ABSOLUTELY, not since the last call, so
        an unabsorbed cent is measured again next time and every time after.
        A model that runs a few cents light per round therefore sat under the
        threshold, invisible, until the accumulated total crossed it in one
        step -- at which point the whole accumulation was rebased into the
        baseline as somebody's deposit. The threshold turned a steady leak
        into a periodic laundering, which is precisely the drift it was meant
        to be too small to cause.

        So the threshold now governs REPORTING only: dust is absorbed
        quietly, a real movement is absorbed and announced. Either way the
        identity above holds on the way out.
        """
        if outstanding is None:
            return 0.0
        tolerance = max(0.01, self._cfg.min_stake_usdt * 0.10)
        if outstanding > tolerance:
            LOG.debug("Not reconciling: %.2f USDT still in flight (staked, "
                      "or won and not yet credited)", outstanding)
            return 0.0
        expected = self._day_start_bankroll + self._realised_pnl
        drift = bankroll - expected
        with self._lock:
            claimable = self._expected_credit
            # Flat, so anything owed has either landed or is not coming. The
            # expectation explains this reconciliation and no later one.
            self._expected_credit = 0.0
        if abs(drift) <= EPS:
            return 0.0

        room = claimable * self._cfg.reconcile_tolerance
        correction = max(-room, min(room, drift))
        if abs(correction) > EPS:
            self._realised_pnl += correction
            self._pnl_correction += correction
            LOG.info("Venue credited %+.2f USDT against what this bot "
                     "calculated; booked as a trading result, not an external "
                     "movement. Today's corrections %+.2f USDT.",
                     correction, self._pnl_correction)
            drift -= correction
        if abs(drift) <= EPS:
            return 0.0

        self._day_start_bankroll = max(self._day_start_bankroll + drift, EPS)
        self._external_flow += drift
        if abs(drift) > tolerance:
            LOG.info("External balance movement %+.2f USDT (deposit, "
                     "withdrawal or an order this bot did not place); "
                     "baseline rebased to %.2f. Not counted as a trading "
                     "result.", drift, self._day_start_bankroll)
        else:
            # Too small to be a trade or a transfer, and still absorbed --
            # see NOTHING IS EVER LEFT OVER above.
            LOG.debug("Absorbed %+.4f USDT of unattributable drift; baseline "
                      "now %.2f", drift, self._day_start_bankroll)
        return drift

    def check(self, bankroll: float,
              outstanding: float | None = None) -> None:
        self._roll_day(bankroll)
        self.reconcile(bankroll, outstanding)
        if self.halted_reason:
            raise TradingHalted(self.halted_reason)

        # Measured on the bot's OWN realised PnL, not on the balance. The two
        # differ by every external flow, and only one of them is the
        # strategy's performance.
        drawdown = -self.realised_pnl / self._day_start_bankroll
        if drawdown >= self._cfg.daily_loss_limit_pct:
            # Report both figures. "Down 35%" invites the question the old
            # message could not answer -- down from what, and did the bot do
            # it? -- and answering it in the halt line is the difference
            # between a diagnosis and a mystery.
            self._halt(f"daily loss limit: {drawdown:.1%} down on this bot's "
                       f"own trades ({self.realised_pnl:+.2f} USDT, including "
                       f"{self.pnl_correction:+.2f} of venue correction; "
                       f"external movements {self.external_flow:+.2f} USDT "
                       f"excluded) "
                       f"(limit {self._cfg.daily_loss_limit_pct:.0%})")
        z = self.calibration_z()
        if z is not None:
            # Enough data for the statistically correct test, so use it and
            # ignore the streak counter entirely. A convex strategy buying
            # 12%-probability contracts hits 30-loss streaks several times per
            # 200 trades; halting on that would shut down a healthy bot.
            if z <= self._cfg.calibration_z_halt:
                self._halt(f"results {abs(z):.1f} sigma below model prediction "
                           f"over {self._samples} trades -- the model is "
                           f"overconfident, not merely unlucky")
        elif self.consecutive_losses >= self._cfg.max_consecutive_losses:
            # Fallback only while the sample is too small to judge properly.
            self._halt(f"{self.consecutive_losses} consecutive losses before "
                       f"enough data to assess calibration; manual review")
        if self.rounds_today >= self._cfg.max_rounds_per_day:
            self._halt("max rounds per day reached")
        if bankroll < self._cfg.min_stake_usdt:
            self._halt("bankroll below minimum stake")

    def _halt(self, reason: str) -> None:
        self.halted_reason = reason
        raise TradingHalted(reason)

    def record_result(self, won: bool,
                      model_prob: float | None = None,
                      pnl: float | None = None) -> None:
        """
        Book a result THIS bot produced.

        pnl is what makes the daily limit meaningful: without it the manager
        knows a trade happened but not what it cost, and has to fall back on
        reading the balance -- which is exactly the conflation this avoids.
        """
        self.rounds_today += 1
        self.consecutive_losses = 0 if won else self.consecutive_losses + 1
        if pnl is not None:
            self._realised_pnl += pnl
        if model_prob is not None:
            self._expected_wins += model_prob
            self._variance += model_prob * (1.0 - model_prob)
            self._actual_wins += int(won)
            self._samples += 1
