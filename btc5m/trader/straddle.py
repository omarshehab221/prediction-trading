"""
Both sides of the same round, sized so the pair pays whichever way it
resolves -- and the rules for a half-straddle waiting for its hedge.
"""

from __future__ import annotations

import math

from btc5m.constants import LOG
from btc5m.domain import Position, Side, Signal, _market_buy
from btc5m.pnl import straddle_worst_case_pnl
from btc5m.pricing import breakeven_probability
from btc5m.sizing import (straddle_completion_band,
                          straddle_completion_stake, straddle_split)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Quote, Round


class StraddleMixin:
    """The straddle strategy."""
    def _straddle_payouts_clear(
            self, raw: Round, prices: dict[Side, float],
            stakes: dict[Side, float],
            total: float) -> tuple[bool, float, str]:
        """
        Would this pair pay back more than it cost, whichever way it lands?

        Returns (ok, worst_case_pnl, reason). `reason` is empty when ok, and
        otherwise names the gate that bound, in the vocabulary _tally_missed
        reports.

        The question is about PAYOUTS, not prices. A leg staking `s` at
        price `p` returns s/p net of fee if it wins, and the pair is only
        worth holding when BOTH of those returns exceed the `total` staked
        across the two -- that is what makes the round's outcome irrelevant,
        which is the entire point of a straddle. straddle_worst_case_pnl
        already computes min(payout_up, payout_down) - total, so the test is
        simply that it come out positive.

        The comparison is strict. "Pays back what it cost" is not the same
        as "pays more than it cost", and at exactly break-even the round is
        capital at risk for nothing.
        """
        for side, price in prices.items():
            if not 0.0 < price <= self._cfg.straddle_max_leg_price:
                return False, 0.0, "straddle leg priced above ceiling"
            if stakes[side] < self._cfg.min_stake_usdt:
                # Reachable through the split, not just through a small
                # bankroll: weighting an asymmetric pair by payout can put
                # the cheap leg under the venue minimum even when the pair
                # as a whole is well funded.
                return False, 0.0, "straddle leg below the venue minimum"

        worst = straddle_worst_case_pnl(
            stakes[Side.UP], stakes[Side.DOWN], prices[Side.UP],
            prices[Side.DOWN], raw.fee_bps)
        if (self._cfg.straddle_require_positive_worst_case
                and worst <= total * self._cfg.straddle_min_worst_case_return):
            return False, worst, "both straddle payouts do not beat the stake"
        return True, worst, ""

    def _completion_is_worth_waiting_out(self, raw: Round, be_open: float,
                                         be_other: float,
                                         seconds_left: float) -> bool:
        """
        Is a price that already locks the round in still worth refusing?

        Any second leg with be_other < 1 - be_open guarantees the round, but
        they are not equally good: cheaper is more profit. Early in a round
        there is time to hold out; as the hedge deadline approaches there is
        not, and a smaller locked-in profit beats an open directional bet.
        The bar slides between the two rather than sitting at either extreme,
        because a fixed high bar strands positions and a fixed low one takes
        the first crumb offered.

        What it slides FROM is the open leg's own fill price. Equal stakes at
        equal prices pay equally, so "a hedge as good as the leg already
        held" is exactly be_open -- the completion price that matches the
        first leg's profit. Deriving it from straddle_first_leg_max_price
        instead, as this used to, tied the bar to the OPENER's ceiling: a leg
        filled at 0.40 would refuse a 0.40 hedge that matched it exactly and
        hold out for a 0.25 bearing no relation to what the round had cost.
        0.25 is a preferred entry price, not a gate on completing.
        """
        preferred = be_open
        limit = 1.0 - be_open
        if preferred >= limit:
            # be_open >= 0.5: the open leg was not cheap enough to be choosy
            # about the other. Anything that locks the round in will do.
            return False
        span = raw.duration_ms / 1000.0 - self._cfg.straddle_hedge_deadline_s
        slack = seconds_left - self._cfg.straddle_hedge_deadline_s
        urgency = (1.0 if span <= 0
                   else 1.0 - max(0.0, min(1.0, slack / span)))
        return be_other > preferred + urgency * (limit - preferred)

    def _open_first_leg(self, raw: Round, legs: dict[Side, float],
                        per_side: float, bankroll: float, mode: str,
                        now_ms: int, since_open: float) -> bool:
        """
        Open whichever side is cheap enough to stand on its own.

        UP and DOWN are interchangeable here and nothing prefers one to the
        other: the side that meets the price is the side that gets bought,
        and the round is completed later from the other end. A round whose
        DOWN goes cheap first is the same trade as one whose UP does.

        Cheap enough means straddle_first_leg_max_price -- 0.40 by default.
        That is not a preference dressed up as a rule: after filling at p,
        the other side stays worth buying all the way up to (1 - p), so a
        first leg at 0.40 leaves 0.60 of room to complete in, while one at
        0.49 leaves almost nothing. It is deliberately looser than the 0.25
        this strategy would prefer, because the opening window is the first
        minute of the round and 0.25 does not exist that early -- what the
        loosened price buys is the four minutes of completion time that make
        the hedge findable at all. Whatever it fills at is then the bar the
        second leg is measured against (_completion_is_worth_waiting_out).

        Returns True if a leg was opened.
        """
        # Whichever is cheaper, with no tie to UP or DOWN.
        side = min(legs, key=legs.get)
        price = legs[side]
        # Both ceilings bind. straddle_max_leg_price is the blunt "never pay
        # more than this for anything" limit; the first-leg ceiling is the
        # strategy's own, and is normally the tighter of the two.
        ceiling = min(self._cfg.straddle_first_leg_max_price,
                      self._cfg.straddle_max_leg_price)
        if not 0.0 < price <= ceiling:
            return False
        free = self._available(bankroll)
        stake = min(per_side, free)
        if stake < self._cfg.min_stake_usdt:
            return False
        if self._cfg.hybrid and free < 2 * self._cfg.min_stake_usdt:
            # A leg the bankroll cannot complete is a directional bet from
            # the moment it fills. Hybrid has a buffer layer for those.
            LOG.debug("%s: %.2f free cannot fund a leg and its partner at "
                      "the %.2f minimum", raw.slug, free,
                      self._cfg.min_stake_usdt)
            return False

        quote = None
        if self._live:
            fresh = self._live_bankroll("straddle first leg")
            if fresh is None or fresh < stake:
                LOG.warning("Straddle %s: %.2f USDT needed to open the %s "
                            "leg but the wallet holds %s", raw.slug, stake,
                            side.value,
                            "an unreadable balance" if fresh is None
                            else "%.2f" % fresh)
                return False
            quote = self._client.get_quote(raw, _market_buy(side, stake))
            if quote.average_price > ceiling:
                LOG.info("Straddle %s: the %s quote at %.4f is dearer than "
                         "the %.2f first-leg ceiling the book suggested; "
                         "not opening", raw.slug, side.value,
                         quote.average_price, ceiling)
                return False
            price = quote.average_price

        self._watching.pop(raw.topic_id, None)
        self._seen[raw.topic_id] = raw.end_ms
        placed = self._place_leg(raw, side, price, stake, quote)
        if placed is None:
            return False              # killed by the venue; nothing opened
        price, stake, order_id = placed
        self._record_straddle_legs(raw, {side: (price, stake, order_id)},
                                   bankroll, mode, now_ms)
        LOG.info("STRADDLE LEG 1 %s | %s %.4f (%.2f) pays %.2f | now hunting "
                 "%s under %.4f to lock the round in (%.0fs since open)",
                 raw.slug, side.value, price, stake,
                 stake / breakeven_probability(price, raw.fee_bps),
                 side.other.value,
                 1.0 - breakeven_probability(price, raw.fee_bps),
                 since_open)
        return True

    def _complete_half_straddles(self, bankroll: float, mode: str,
                                 now_ms: int) -> None:
        """
        Buy the missing side of any round holding only one leg.

        This is the half of the strategy that makes it a strategy. The two
        sides of a live book sum to about 1.00, so a pair bought in one
        instant is nearly never profitable both ways -- but the sides move
        independently over the five minutes of a round, and a leg bought at
        0.25 early can be joined by the other side at 0.25 a minute later.
        Neither price was ever available alongside the other.

        Runs before any new round is opened, and is not subject to
        max_concurrent_positions: completing a hedge REMOVES exposure, and
        starving it of a slot in favour of opening fresh directional legs is
        exactly backwards.
        """
        for (symbol, side), pos in list(self._positions.items()):
            other = side.other
            if (symbol, other) in self._positions:
                continue
            if (self._cfg.hybrid
                    and pos.trade_id not in self._hybrid_straddle_legs):
                # A buffer position is one-sided by design, not half of a
                # pair. "Completing" a 0.70 buffer bet whose other side fell
                # to 0.20 turns a +43% winner into a small locked profit.
                continue
            raw = pos.rnd
            seconds_left = raw.seconds_remaining(now_ms)
            if seconds_left <= 0:
                continue

            price = self._book_price(raw, other)
            if price is None:
                # Late in a round the missing side is routinely quoted at
                # 0.00 or 1.00. Neither is completable; wait for a real one.
                LOG.debug("%s: no usable %s price to complete with yet",
                          raw.slug, other.value)
                continue
            budget = self._available(bankroll)
            stake, guaranteed = straddle_completion_stake(
                pos.committed_usdt, pos.signal.fill_price, price,
                raw.fee_bps, budget)
            if (self._cfg.hybrid and stake < self._cfg.min_stake_usdt
                    <= budget):
                # Floored to the minimum -- and re-tested, because a larger
                # second leg can overshoot the band's ceiling, and then it is
                # not a lock but a second directional bet.
                stake = self._cfg.min_stake_usdt
                lo, hi = straddle_completion_band(
                    pos.committed_usdt, pos.signal.fill_price, price,
                    raw.fee_bps)
                guaranteed = lo < stake < hi
            past_deadline = seconds_left <= self._cfg.straddle_hedge_deadline_s

            if guaranteed:
                if self._completion_is_worth_waiting_out(
                        raw, breakeven_probability(pos.signal.fill_price,
                                                   raw.fee_bps),
                        breakeven_probability(price, raw.fee_bps),
                        seconds_left):
                    LOG.debug("%s: %s at %.4f would lock the round in, but "
                              "there is still time to want it cheaper",
                              raw.slug, other.value, price)
                    continue
            elif not past_deadline:
                LOG.debug("%s: %s at %.4f cannot cover the %.2f already on "
                          "%s (%.0fs left to find a price that can)",
                          raw.slug, other.value, price, pos.committed_usdt,
                          side.value, seconds_left)
                continue
            elif not self._cfg.straddle_force_hedge:
                continue
            else:
                LOG.warning(
                    "Straddle %s: no price for %s ever covered the %.2f on "
                    "%s, and the round closes in %.0fs. Hedging at %.4f "
                    "anyway -- the guaranteed profit is gone, but this is a "
                    "bounded loss instead of an all-or-nothing bet.",
                    raw.slug, other.value, pos.committed_usdt, side.value,
                    seconds_left, price)

            if stake < self._cfg.min_stake_usdt:
                LOG.debug("%s: completing %s needs %.2f, under the %.2f "
                          "venue minimum", raw.slug, other.value, stake,
                          self._cfg.min_stake_usdt)
                continue

            quote = None
            if self._live:
                fresh = self._live_bankroll("straddle completion")
                if fresh is None or fresh < stake:
                    LOG.warning("Straddle %s: %.2f USDT needed to complete "
                                "the %s side but the wallet holds %s",
                                raw.slug, stake, other.value,
                                "an unreadable balance" if fresh is None
                                else "%.2f" % fresh)
                    continue
                quote = self._client.get_quote(raw, _market_buy(other, stake))
                # Re-tested, NOT re-sized. place_order executes this quote's
                # id, which is bound to the size it was asked for, so the
                # only honest question is whether THIS trade still locks the
                # round in at the price it will actually fill at.
                if (straddle_worst_case_pnl(
                        pos.committed_usdt, stake, pos.signal.fill_price,
                        quote.average_price, raw.fee_bps) <= 0
                        and not (past_deadline
                                 and self._cfg.straddle_force_hedge)):
                    LOG.info("Straddle %s: the %s quote at %.4f no longer "
                             "covers the %.2f open on %s; still hunting",
                             raw.slug, other.value, quote.average_price,
                             pos.committed_usdt, side.value)
                    continue

            placed = self._place_leg(raw, other, price, stake, quote)
            if placed is None:
                # The hedge was killed, so the open leg is still open. Left
                # for the next poll rather than given up on.
                continue
            price, stake, order_id = placed
            self._record_straddle_legs(raw, {other: (price, stake, order_id)},
                                       bankroll, mode, now_ms)
            total = pos.committed_usdt + stake
            worst = straddle_worst_case_pnl(
                pos.committed_usdt, stake, pos.signal.fill_price, price,
                raw.fee_bps)
            LOG.info("STRADDLE COMPLETE %s | %s %.4f (%.2f) then %s %.4f "
                     "(%.2f) | either outcome pays %.2f on %.2f staked, "
                     "worst case %+.2f (%.0fs before close)",
                     raw.slug, side.value, pos.signal.fill_price,
                     pos.committed_usdt, other.value, price, stake,
                     total + worst, total, worst, seconds_left)

    def _record_straddle_legs(
            self, raw: Round,
            filled: dict[Side, tuple[float, float, str | None]],
            bankroll: float, mode: str, now_ms: int) -> None:
        """Journal every leg that actually reached the venue."""
        for side, (price, stake, order_id) in filled.items():
            # model_prob/edge are meaningless for a strategy with no
            # probability model; a neutral 0.5 keeps the journal schema and
            # calibration_report's bucketing arithmetic valid without
            # implying a directional forecast that was never made. edge is
            # left as a simple descriptive read of how far the fill sat from
            # a coin-flip breakeven -- not a signal this profile acted on.
            sig = Signal(side, model_prob=0.5, fill_price=price,
                         edge=0.5 - breakeven_probability(price, raw.fee_bps),
                         stake_usdt=stake,
                         seconds_left=raw.seconds_remaining(now_ms))
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id)
            # The shares the venue delivered, when it says. A straddle leg is
            # never sold -- it rides to settlement and is redeemed -- and the
            # claim is sized from the shares held, so without this the
            # cost-implied count stands in and declares a credit larger than
            # the venue owes by the buy's fee.
            shares = (self._client.delivered_shares(order_id)
                      if self._live and order_id else None)
            self._positions[(raw.symbol, side)] = Position(
                tid, raw, sig, stake, 1, shares=shares)
            if self._cfg.hybrid:
                self._hybrid_straddle_legs.add(tid)

    def _maybe_enter_straddle(self, bankroll: float, mode: str) -> None:
        """
        Buy both sides of a round -- usually at two different moments.

        No model probability and no side is picked. The one thing asked of a
        round is the payout test: whichever way it settles, the winning leg
        must return more than the pair cost together. That makes direction
        genuinely irrelevant, which is the only footing on which buying both
        sides makes sense.

        The catch is that a live book prices the two sides to sum to about
        1.00, so a pair bought in ONE instant almost never passes that test.
        Bought at two instants it can: the sides move independently over the
        five minutes of a round, so UP at 0.25 while spot slides and DOWN at
        0.25 after it bounces are both real prices that were simply never on
        offer at the same time. So there are two ways in:

          * both sides clear together right now -- taken immediately, sized
            by straddle_split, with no legging risk at all. Rare.
          * one side is cheap enough on its own
            (straddle_first_leg_max_price) -- opened alone, and completed
            later by _complete_half_straddles once the other side becomes
            worth buying, where "worth" means as good as the price the open
            leg itself got. That is the path this profile actually trades.

        Opening is confined to straddle_entry_window_s -- the opening
        stretch of the round, so the rest of it is completion time.
        Completing is not, and runs until straddle_hedge_deadline_s before
        settlement.

        Legs are separate MARKET FOK orders -- this venue has no limit order
        type -- so in live mode every leg is quoted and re-tested against the
        price that will actually execute before the order goes out.
        """
        now_ms = self._client.now_ms()
        self._prune(now_ms)
        # Before anything else, and deliberately outside the concurrency cap:
        # an open leg with no partner is the only directional exposure this
        # profile ever carries, and closing that gap beats opening new rounds
        # every time.
        self._complete_half_straddles(bankroll, mode, now_ms)

        if len(self._positions) >= self._cfg.max_concurrent_positions:
            return

        available = self._available(bankroll)
        per_side = bankroll * self._cfg.straddle_stake_pct
        if self._cfg.hybrid and per_side < self._cfg.min_stake_usdt:
            # Floored, not refused: see the hybrid profile. The warning below
            # can then only fire for the plain straddle profile.
            LOG.debug("Straddle leg %.2f (%.0f%% of %.2f) floored to the "
                      "%.2f minimum", per_side,
                      self._cfg.straddle_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt)
            per_side = self._cfg.min_stake_usdt
        # Two different conditions, and conflating them is what hid the
        # original bug. Sizing below the venue minimum is a configuration
        # fault that will never clear on its own, so it is loud. Capital
        # already committed to another round is the ordinary state of a
        # profile holding positions, so it stays at debug.
        if per_side < self._cfg.min_stake_usdt:
            msg = ("Straddle cannot enter any round: %.2f USDT per leg "
                   "(%.1f%% of a %.2f bankroll) is below the %.2f venue "
                   "minimum. Raise straddle_stake_pct or fund the wallet -- "
                   "nothing will be traded until one of those changes."
                   % (per_side, self._cfg.straddle_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt))
            if msg != self._idle_reason:
                self._idle_reason = msg
                LOG.warning("%s", msg)
            return
        self._idle_reason = ""
        if available < per_side * 2:
            LOG.debug("No uncommitted bankroll for a straddle (%.2f needed, "
                      "%.2f available after the %.0f%% reserve and %.2f "
                      "already committed)", per_side * 2, available,
                      self._cfg.reserve_pct * 100, self._committed())
            return

        for raw in self._list_rounds():
            if raw.topic_id in self._seen:
                continue
            # A market already holding a leg belongs to
            # _complete_half_straddles, which ran above.
            if any(k[0] == raw.symbol for k in self._positions):
                continue

            since_open = (now_ms - raw.start_ms) / 1000.0
            if since_open < 0.0:
                continue                      # has not opened yet
            # Two bounds, and once either binds the round is finished for
            # opening purposes. The window caps how late a first leg may be
            # started; the runway floor refuses one that could never be
            # hedged -- completion stops at straddle_hedge_deadline_s and
            # needs time to work before then, so it is derived from that
            # rather than being another number to keep in sync.
            runway = self._cfg.straddle_hedge_deadline_s * 2.0
            if (since_open > self._cfg.straddle_entry_window_s
                    or raw.seconds_remaining(now_ms) <= runway):
                # Hybrid hands this round to the buffer layer, which reads
                # the same _seen: writing it off here would mean buffer never
                # sees a round at all. A round a leg was OPENED on is still
                # written off, by _open_first_leg, and stays off to both.
                if not self._cfg.hybrid:
                    self._seen[raw.topic_id] = raw.end_ms
                continue

            priced = {side: self._book_price(raw, side)
                      for side in (Side.UP, Side.DOWN)}
            if any(price is None for price in priced.values()):
                LOG.debug("%s: no usable two-sided price (UP %s / DOWN %s)",
                          raw.slug, priced[Side.UP], priced[Side.DOWN])
                continue
            legs: dict[Side, float] = {side: price
                                       for side, price in priced.items()
                                       if price is not None}

            # Recomputed per round, not once per pass: entering one round
            # commits capital that the next round in the same pass must not
            # be sized against as though it were still free.
            free = self._available(bankroll)
            total = min(per_side * 2.0, free)
            stakes = dict(zip((Side.UP, Side.DOWN),
                              straddle_split(total, legs[Side.UP],
                                             legs[Side.DOWN], raw.fee_bps)))
            if self._cfg.hybrid:
                cheap = min(stakes.values())
                if 0 < cheap < self._cfg.min_stake_usdt:
                    # The payout weighting put the cheap leg under the
                    # minimum. Scale the pair, not the leg -- scaling one leg
                    # breaks the equal payouts the split exists for. A pair
                    # the free balance cannot scale stays as it was, and the
                    # payout check refuses it for the leg under the minimum.
                    scaled = total * self._cfg.min_stake_usdt / cheap
                    if scaled <= free:
                        total = scaled
                        stakes = dict(zip((Side.UP, Side.DOWN),
                                          straddle_split(total, legs[Side.UP],
                                                         legs[Side.DOWN],
                                                         raw.fee_bps)))
            ok, worst, reason = self._straddle_payouts_clear(
                raw, legs, stakes, total)
            if not ok:
                LOG.debug("%s: %s (UP %.4f / DOWN %.4f, %.2f + %.2f staked)",
                          raw.slug, reason, legs[Side.UP], legs[Side.DOWN],
                          stakes[Side.UP], stakes[Side.DOWN])
                # The normal case, not a failure: the two sides of one book
                # sum to about 1.00. Take whichever side is cheap enough to
                # stand on its own and let the completion pass find the
                # other one at a price that never coexisted with this one.
                if self._open_first_leg(raw, legs, per_side, bankroll, mode,
                                        now_ms, since_open):
                    continue
                self._watching[raw.topic_id] = (raw.end_ms, reason)
                continue

            # Live pricing is a second opinion that can disagree with the
            # book: a quote average price carries the impact of THIS size,
            # which the top-of-book level does not. Both quotes are taken
            # BEFORE either order is placed -- quotes are non-binding and
            # place nothing -- and the gate is then re-tested against the
            # prices that will actually execute. Testing the book but
            # executing the quote is how a pair that cleared on paper turns
            # into a guaranteed loss in the account.
            quotes: dict[Side, Quote] = {}
            if self._live:
                fresh = self._live_bankroll("straddle entry")
                if fresh is None or fresh < total:
                    LOG.warning("Straddle %s: %.2f USDT needed for the pair "
                                "but the wallet holds %s; entering neither "
                                "side", raw.slug, total,
                                "an unreadable balance" if fresh is None
                                else "%.2f" % fresh)
                    continue
                for side in (Side.UP, Side.DOWN):
                    quotes[side] = self._client.get_quote(
                        raw, _market_buy(side, stakes[side]))
                quoted = {side: q.average_price for side, q in quotes.items()}
                ok, worst, reason = self._straddle_payouts_clear(
                    raw, quoted, stakes, total)
                if not ok:
                    LOG.info("Straddle %s: %s once quoted (UP %.4f / DOWN "
                             "%.4f against a book of %.4f / %.4f); entering "
                             "neither side", raw.slug, reason,
                             quoted[Side.UP], quoted[Side.DOWN],
                             legs[Side.UP], legs[Side.DOWN])
                    self._watching[raw.topic_id] = (raw.end_ms, reason)
                    continue
                legs = quoted

            self._watching.pop(raw.topic_id, None)
            self._seen[raw.topic_id] = raw.end_ms

            # try/finally, not a plain loop: once the first order is placed
            # that money is committed whatever happens to the second, and an
            # earlier version discarded the whole filled map on any abort --
            # leaving a real, unhedged live position that was never
            # journalled, never settled and never claimed. Anything that
            # filled gets recorded before the failure is allowed to surface.
            filled: dict[Side, tuple[float, float, str | None]] = {}
            try:
                for side in (Side.UP, Side.DOWN):
                    placed = self._place_leg(raw, side, legs[side],
                                             stakes[side],
                                             quotes.get(side))
                    if placed is None:
                        continue     # killed by the venue; no position
                    filled[side] = placed
            finally:
                # Inside the finally, so a leg that reached the venue is
                # journalled even when the exception from the other one is
                # about to unwind this whole call.
                self._record_straddle_legs(raw, filled, bankroll, mode,
                                           now_ms)
                if len(filled) == 1:
                    only = next(iter(filled))
                    LOG.error("Straddle %s: only the %s leg opened. This "
                              "round is now a one-sided directional bet, "
                              "not a hedge, and the payout gate no longer "
                              "holds.", raw.slug, only.value)

            if len(filled) == 2:
                LOG.info("STRADDLE %s | UP %.4f (%.2f) / DOWN %.4f (%.2f) | "
                         "either outcome pays %.2f on %.2f staked, worst "
                         "case %+.2f (%.0fs since open)",
                         raw.slug, legs[Side.UP], filled[Side.UP][1],
                         legs[Side.DOWN], filled[Side.DOWN][1],
                         total + worst, total, worst, since_open)

            if len(self._positions) >= self._cfg.max_concurrent_positions:
                return
