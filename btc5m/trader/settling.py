"""What the round paid, and whether the account agrees."""

from __future__ import annotations

from btc5m.constants import EPS, LOG
from btc5m.domain import Side
from btc5m.pnl import settle_pnl

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Position


class SettlementMixin:
    """Settlement and reconciliation against the venue."""
    def _settle_open(self) -> None:
        for key in list(self._positions):
            self._settle_one(key)

    def _settle_one(self, key: tuple[str, Side]) -> None:
        pos = self._positions.get(key)
        if pos is None:
            return
        now_ms = self._client.now_ms()
        if now_ms < pos.rnd.end_ms + self._cfg.settle_grace_s * 1000:
            return

        winner: Side | None = None
        pnl: float | None = None
        source = "venue"

        settled = self._client.settled_outcome(pos.rnd)
        if settled is not None:
            winner, venue_pnl = settled
            if self._live and venue_pnl is not None:
                pnl = venue_pnl
        else:
            final = self._client.final_price(pos.rnd)
            if final is not None and pos.rnd.strike is not None:
                if final == pos.rnd.strike:
                    # Unverified tie rule: do not guess on a real position.
                    LOG.warning("%s closed exactly at the strike (%.8f); the "
                                "venue's tie rule is unknown, so this is left "
                                "for venue settlement rather than guessed",
                                pos.rnd.slug, final)
                    return
                winner = Side.UP if final > pos.rnd.strike else Side.DOWN
                source = "endPrice"

        if winner is None:
            if now_ms > pos.rnd.end_ms + self._cfg.settle_timeout_s * 1000:
                LOG.error("Cannot settle %s; left unresolved in journal",
                          pos.rnd.slug)
                # Only THIS leg, not every open position -- a straddle round
                # can have one leg settle cleanly while the other's outcome
                # lookup is still stuck, and clearing both would abandon a
                # leg that was never actually resolved.
                self._positions.pop(key, None)
            return

        won = winner is pos.signal.side
        if pnl is None:
            # What is still at risk, not the original stake. Every entry path
            # keeps the two equal; only a partial sale shrinks committed_usdt,
            # and max() with the stake then settled the sold part a second
            # time -- a remnant logged "WIN P&L +1.06" after being sold.
            pnl = settle_pnl(pos.committed_usdt, pos.signal.fill_price, won,
                             pos.rnd.fee_bps)
        if not self._live:
            self._paper_bankroll += pnl

        # Read the balance before claiming so the change can be reconciled
        # against what we expected, rather than inferred.
        before: float | None = None
        if self._live:
            before = self._live_bankroll("settlement")

        if won and self._live:
            # Say what is owed before handing it to the claim worker. The
            # credit lands minutes later and may land light; declaring it is
            # what lets reconciliation read a shortfall as this trade's
            # result rather than as a stranger's withdrawal.
            # A winning share redeems for 1 USDT, so the payout is the shares
            # still held -- not the original stake over the entry price.
            payout = pos.held_shares
            self._risk_for(key[0]).expect_credit(payout)
            if self._account_risk is not None:
                self._account_risk.expect_credit(payout)
            self._claim(pos)

        self._journal.resolve(pos.trade_id, won, pnl, source)
        self._risk_for(key[0]).record_result(won, pos.signal.model_prob, pnl)
        if self._account_risk is not None:
            self._account_risk.record_result(won, pos.signal.model_prob, pnl)
        self._positions.pop(key, None)
        after = self._bankroll()
        LOG.info("SETTLED %s -> %s  P&L %+.2f  bankroll %.2f  [%s]",
                 pos.rnd.slug, "WIN" if won else "LOSS", pnl, after, source)
        if before is not None:
            self._reconcile(pos, won, pnl, before, after)

        self._settled_count += 1
        if (self._cfg.report_every
                and self._settled_count % self._cfg.report_every == 0):
            for line in self._journal.calibration_report(
                    self._cfg.profile_name).split("\n"):
                LOG.info("| %s", line)

    def _reconcile(self, pos: Position, won: bool, expected_pnl: float,
                   before: float, after: float) -> None:
        """
        Compare the actual balance change against what settling should move.

        The bot's own arithmetic and the venue's accounting should agree. When
        they do not, the venue is right and something here is wrong -- a fee we
        did not model, a partial fill, or a figure counted twice. Reporting the
        gap turns a silent drift into a visible one.

        WHAT SETTLING IS ACTUALLY EXPECTED TO MOVE
        -----------------------------------------
        Not the P&L. The stake left the balance at ENTRY, so a losing round
        moves the balance by nothing at all when it settles -- there is
        nothing left to lose. Comparing the balance delta against a P&L of
        -stake made every single loss report a mismatch the size of the whole
        stake, complete with a hint that something was double-counting it.
        Nothing was; the two numbers were simply measuring different events.

        A win moves the balance by the GROSS payout, and only once the claim
        has been credited on chain -- which is normally after this runs, so
        that case is noted rather than flagged.

        WHY THIS ONLY REPORTS, AND DOES NOT BOOK
        ----------------------------------------
        It briefly did book the gap against PnL, on the reasoning that two
        balance reads seconds apart cannot bracket a deposit. The reads are
        that close together; they are not that clean, and the arithmetic here
        was wrong in a way that cost real money.

        A WIN is the fatal case. Its payout lands minutes later, so `actual`
        is near zero here while `expected_move` is the whole gross payout,
        and the only thing standing between that and a correction of MINUS
        THE ENTIRE PAYOUT was a guard requiring the balance to be unchanged
        to the last EPS. Anything at all moving in the window defeats it --
        an earlier claim landing, dust, or the other leg of the very same
        straddle round settling a moment before, which is not an edge case
        but the normal shape of every straddle. A won round could book its
        payout into PnL and then immediately subtract it again.

        A LOSS is only better by degree: expected_move is zero, so any
        unrelated credit landing inside the window reads as this round's
        modelling error.

        The window is too narrow for a deposit and still too wide for
        attribution, which leaves this measurement useful as a SIGNAL and
        unfit as a LEDGER ENTRY. So it warns, and the books are squared where
        the measurement is unambiguous instead: RiskManager.reconcile, which
        runs only with a flat book and nothing in flight, and which anchors
        the baseline to the venue's balance so that no residue survives to
        accumulate. The venue is still the single source of truth -- this is
        just not the place that reads it.
        """
        actual = after - before
        # A loss should move nothing: the money went out when the order did.
        expected_move = (pos.committed_usdt / max(pos.signal.fill_price, EPS)
                         if won else 0.0)
        reference = max(abs(expected_move), self._cfg.min_stake_usdt)
        drift = abs(actual - expected_move)

        if won:
            # The claim is queued, not credited. There is nothing to compare
            # against yet and there will not be before this returns.
            LOG.debug("Settled a win on %s; payout %.4f not yet credited, "
                      "balance moved %+.4f", pos.rnd.slug, expected_move,
                      actual)
            return
        if drift <= reference * self._cfg.reconcile_tolerance:
            LOG.debug("Reconciled a loss: expected the balance to move "
                      "%+.4f, it moved %+.4f (P&L %+.4f)",
                      expected_move, actual, expected_pnl)
            return

        LOG.warning(
            "RECONCILE MISMATCH on %s: settling a loss should have moved the "
            "balance %+.4f, it moved %+.4f (gap %.4f, P&L %+.4f). Either a "
            "cost is unmodelled here or something landed in the window. Not "
            "booked -- the balance is squared against the venue when the book "
            "next goes flat.",
            pos.rnd.slug, expected_move, actual, drift, expected_pnl)
