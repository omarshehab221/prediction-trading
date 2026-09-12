"""
Forgetting rounds that are over, and saying why the ones that passed
were never entered.
"""

from __future__ import annotations

from btc5m.constants import LOG


class BookkeepingMixin:
    """Pruning expired state and tallying missed rounds."""
    def _prune(self, now_ms: int) -> None:
        horizon = now_ms - self._cfg.prune_after_s * 1000
        for tid in [t for t, end in self._seen.items() if end < horizon]:
            self._seen.pop(tid, None)
            self._hydrated.pop(tid, None)
        # The scalp counters are keyed by topic and are deliberately NOT
        # written to _seen -- that would cap this profile at one trade in the
        # five minutes it exists to trade repeatedly -- so they carry their
        # own end_ms and get their own sweep. Without it they grow by one
        # entry per round for the life of the process.
        for tid in [t for t, (end, _) in self._scalp_entries.items()
                    if end < horizon]:
            self._scalp_entries.pop(tid, None)
            self._flattened.discard(tid)

    def _tally_missed(self, now_ms: int) -> None:
        """
        Count rounds that expired without a trade, by what blocked them.

        Silence is the failure mode this exists to prevent. With a return
        floor in force, "no trade" is the correct answer surprisingly often,
        and it is indistinguishable from a broken endpoint or a stale book
        unless the reason is written down. A periodic summary makes the
        difference between "the market never offered a price worth taking"
        and "the buffer gate is set too high to ever fire" visible without
        having to read a debug log.
        """
        expired = [t for t, (end, _) in self._watching.items() if end < now_ms]
        for tid in expired:
            _, reason = self._watching.pop(tid)
            self._missed[reason] = self._missed.get(reason, 0) + 1
            self._missed_total += 1

        if not expired or self._missed_total % 25 != 0:
            return
        ranked = sorted(self._missed.items(), key=lambda kv: -kv[1])
        LOG.info("No trade in %d round(s) so far: %s", self._missed_total,
                 "; ".join(f"{n} {reason}" for reason, n in ranked[:4]))
        top = ranked[0][0]
        if top == "win pays less than the return floor":
            LOG.info("  The prices on offer were fine bets but small wins. "
                     "Lower min_win_return to trade more of them, "
                     "understanding that is the trade you asked not to make.")
        elif top == "buffer too small for the time left":
            LOG.info("  Spot is not moving far enough from the strike. "
                     "Lower min_buffer_sigmas, or accept fewer setups.")
        elif top == "both straddle payouts do not beat the stake":
            LOG.info("  The two sides are priced to sum to 1.00 or more, so "
                     "buying both is a guaranteed loss. This is the normal "
                     "resting state of the straddle profile -- it is "
                     "refusing rounds, not failing to see them. Nothing to "
                     "fix unless it never clears.")
        elif top == "straddle leg below the venue minimum":
            LOG.info("  Sizing the pair by payout put one leg under the "
                     "venue minimum. Raise straddle_stake_pct, or fund the "
                     "wallet, so the cheap side still clears it.")
        elif top == "no side has reached the price floor":
            LOG.info("  The leader never reached %.2f while the floor was "
                     "still in force. Reaching expiry on that reason also "
                     "means the fallback never got a look -- no poll landed "
                     "inside its window, usually because the position slots "
                     "were full or the market was halted. Lower "
                     "last_minute_price_floor to take more of these before "
                     "the fallback has to.",
                     self._cfg.last_minute_price_floor)
        elif top == "the leading side is priced above the ceiling":
            LOG.info("  The rounds reaching the last minute were already "
                     "decided, and %.2f is the most this profile will pay "
                     "for one. That is the ceiling doing its job, not a "
                     "fault. Raise last_minute_max_price to take them, "
                     "understanding a win at 0.97 pays about 3%%.",
                     self._cfg.last_minute_max_price)
        elif top == "the two sides are priced level":
            LOG.info("  The book could not separate UP from DOWN in the last "
                     "minute. There is no dominant side to buy in that, and "
                     "picking one anyway would be inventing a signal. This "
                     "is the profile refusing rounds, not failing to see "
                     "them.")
        elif top == "edge below the floor":
            LOG.info("  The venue is pricing these rounds close to the "
                     "model. That is a market with no edge in it, not a "
                     "misconfiguration.")
