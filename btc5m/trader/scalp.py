"""
Trading the perp/spot dislocation: the futures lead, its bracket and
the flattening that closes the round out.
"""

from __future__ import annotations

import math
import time

import requests

from btc5m.constants import (
    BASIS_EWMA_ALPHA,
    BASIS_EWMA_MIN_SAMPLES,
    DUST_USDT,
    EPS,
    LOG,
)
from btc5m.domain import (
    Action,
    Bracket,
    OrderPlan,
    OrderType,
    PendingOrder,
    Position,
    Side,
    Signal,
    _market_buy,
)
from btc5m.errors import ApiError, TradingHalted
from btc5m.pricing import (
    bracket_prices,
    breakeven_probability,
    signal_edge_required,
)


class ScalpMixin:
    """The futures-lead scalp strategy and its brackets."""
    def _basis_dislocation_bps(self, symbol: str) -> float | None:
        """
        How far the perp/spot basis sits from its own recent mean, in bps.

        The LEVEL of the basis says nothing about whether spot is lagging: it
        is dominated by funding, which is a persistent bias and not
        information. Gating on it would fire constantly on one side and never
        on the other -- a funding-rate detector wearing a lead-lag costume.
        The DEVIATION from its own recent mean is the quantity the premise
        actually describes: the perp has moved and spot has not followed yet.

        The mean is an EWMA advanced once per pass, which is the cheapest
        estimator that does not need a second ring buffer. The deviation is
        measured against the mean BEFORE this sample is folded in, or every
        observation would be partly averaged into its own baseline and the
        dislocation would report smaller than it is.

        None until the EWMA has seen enough samples to be a mean rather than
        the first observation restated -- and None, not 0.0, because "no
        dislocation" and "no idea" must not read alike to the caller.
        """
        perp = self._market_data.futures_mid(symbol)
        if perp is None or perp <= 0:
            return None
        try:
            spot = self._market_data.spot(symbol)
        except (ApiError, requests.RequestException) as exc:
            LOG.debug("%s: no spot to measure the basis against: %s",
                      symbol, exc)
            return None
        if spot <= 0:
            return None
        basis = (perp - spot) / spot * 10_000.0
        mean, seen = self._basis_ewma.get(symbol, (basis, 0))
        self._basis_ewma[symbol] = (
            mean + (basis - mean) * BASIS_EWMA_ALPHA, seen + 1)
        if seen < BASIS_EWMA_MIN_SAMPLES:
            return None
        return basis - mean

    def _scalp_signal(self, symbol: str) -> tuple[Side, float, float] | None:
        """
        Which way the perp is going, or None when there is nothing to trade.

        Returns (side, perp move in bps, basis dislocation in bps).

        Two conditions, both required. The first is that the perp has moved
        at all; the second is that spot has not followed it yet, which is
        the half that makes this a lead rather than momentum. Setting
        scalp_min_basis_bps to 0 drops the second and leaves a pure momentum
        strategy -- a real setting, and a different one, so it is opted into
        rather than arrived at.

        The freshness check is per SYMBOL and cannot be delegated to the
        socket's own health. That flag asks whether any frame arrived
        recently across every subscription, so a busy BTC stream keeps it
        green while a quiet market's newest tick is a minute old -- and a
        minute-old tick is not a millisecond lead, it is history.
        """
        cfg = self._cfg
        age = self._market_data.futures_tick_age_ms(symbol)
        if age is None or age > cfg.scalp_max_tick_age_ms:
            return None
        # Sample the basis on EVERY pass, before the move gate. The mean it
        # is measured against needs BASIS_EWMA_MIN_SAMPLES samples; fed only
        # when the perp had already moved, every restart spent the first
        # twenty real moves warming up and traded none of them -- and the
        # mean it learned was a mean of moves, not of the basis.
        dislocation = (self._basis_dislocation_bps(symbol)
                       if cfg.scalp_min_basis_bps > 0 else 0.0)
        move = self._market_data.futures_move_bps(symbol, cfg.scalp_lookback_ms)
        if move is None or abs(move) < cfg.scalp_min_move_bps:
            return None
        side = Side.UP if move > 0 else Side.DOWN
        if cfg.scalp_min_basis_bps <= 0:
            return side, move, 0.0
        if dislocation is None:
            return None
        # Same sign as the move, by at least the threshold. A perp that has
        # risen while the basis RICHENED means spot is behind; one that has
        # risen while the basis cheapened means spot has already caught up
        # and overtaken, and the thing being chased is over.
        if move > 0 and dislocation < cfg.scalp_min_basis_bps:
            return None
        if move < 0 and dislocation > -cfg.scalp_min_basis_bps:
            return None
        return side, move, dislocation

    def _maybe_enter_scalp(self, bankroll: float, mode: str) -> None:
        """
        Buy the side the perp is moving toward, and bracket it immediately.

        No model, no strike, no volatility, no buffer. This path never asks
        which side wins the round -- it asks which way this token's PRICE is
        about to move, over the next few seconds, and takes a fixed profit or
        a fixed loss either way.

        A round is entered repeatedly, so _seen is deliberately never
        written. Three bounds replace it: a per-round ceiling, a per-symbol
        cooldown, and the requirement that the symbol be completely flat --
        no position and no resting order. Stacking a second entry on a live
        bracket would leave the stop watching a price that neither fill
        chose.
        """
        cfg = self._cfg
        now_ms = self._client.now_ms()
        self._prune(now_ms)
        if len(self._positions) >= cfg.max_concurrent_positions:
            return

        target = bankroll * cfg.scalp_stake_pct
        if self._available(bankroll) < cfg.min_stake_usdt:
            LOG.debug("No uncommitted bankroll for a scalp (%.2f committed "
                      "of %.2f)", self._committed(), bankroll)
            return

        now_s = time.monotonic()
        for raw in self._client.list_rounds():
            secs = raw.seconds_remaining(now_ms)
            if not (cfg.entry_window_end_s <= secs <= cfg.entry_window_start_s):
                continue
            # Flat, on both counts. A pending order on this round is a
            # bracket leg or an entry still resolving; either way the symbol
            # is not free.
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            if any(p.rnd.symbol == raw.symbol for p in self._pending.values()):
                continue
            try:
                self._risk_for(raw.symbol).check(bankroll)
            except TradingHalted as exc:
                LOG.debug("%s halted: %s", raw.symbol, exc)
                continue

            end_ms, taken = self._scalp_entries.get(raw.topic_id,
                                                   (raw.end_ms, 0))
            if taken >= cfg.scalp_max_entries_per_round:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the round's scalp ceiling is reached")
                continue
            last = self._scalp_last_entry.get(raw.symbol)
            if last is not None and now_s - last < cfg.scalp_cooldown_s:
                continue

            if cfg.min_liquidity > 0 and (raw.liquidity is None
                                          or raw.liquidity < cfg.min_liquidity):
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the book is thinner than the minimum")
                continue

            # The bracket has to be worth placing before a quote is spent on
            # finding out. This is a property of the market's fee and the two
            # targets, so it can be answered before anything is asked of the
            # venue.
            required = signal_edge_required(raw.fee_bps,
                                            cfg.scalp_take_profit_pct,
                                            cfg.scalp_stop_loss_pct)
            if required is None or required > cfg.scalp_max_edge_required:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the fee demands more signal than the "
                                "bracket can carry")
                LOG.debug("%s: a %d bps fee needs %s hit-rate points over a "
                          "coin flip, above the %.2f ceiling", raw.slug,
                          raw.fee_bps,
                          "no reachable" if required is None
                          else f"{required:.2f}", cfg.scalp_max_edge_required)
                continue

            symbol = self._client.market_symbol(raw.feed_symbol)
            signal = self._scalp_signal(symbol)
            if signal is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the futures feed shows no lead to trade")
                continue
            side, move_bps, dislocation = signal

            # The fraction, floored at the venue minimum: a small bankroll
            # still takes one minimum-size order instead of sitting out every
            # signal. The floor never reaches past what is free to spend, so
            # the reserve and open positions still bound it.
            stake = min(max(target, cfg.min_stake_usdt),
                        self._available(bankroll))
            if stake < cfg.min_stake_usdt:
                continue

            quote = None
            price = self._book_price(raw, side)
            if price is None:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the side has no tradable price")
                continue
            if self._live:
                fresh = self._live_bankroll("scalp entry")
                if fresh is None:
                    continue
                stake = min(stake, fresh)
                if stake < cfg.min_stake_usdt:
                    LOG.warning("%s: the wallet holds %.2f, under the %.2f "
                                "minimum order; skipping", raw.slug, fresh,
                                cfg.min_stake_usdt)
                    continue
                quote = self._client.get_quote(raw, _market_buy(side, stake))
                # The quote is authoritative; the book was a screen. Every
                # gate below binds on the price that will actually execute,
                # because the bracket is computed from that price and a
                # bracket built on a screen price protects nothing.
                price = quote.average_price
                if abs(quote.price_impact) > cfg.max_price_impact:
                    LOG.info("%s: price impact %.1f%% too high for a scalp; "
                             "skipping", raw.slug, quote.price_impact * 100)
                    continue
            if not cfg.min_entry_price <= price <= cfg.max_entry_price:
                self._watching[raw.topic_id] = (
                    raw.end_ms, "the side is priced outside the band")
                continue

            bracket = bracket_prices(price, raw.fee_bps,
                                     cfg.scalp_take_profit_pct,
                                     cfg.scalp_stop_loss_pct)
            if bracket is None:
                # Usually the take-profit landing at or above 1.00: this fill
                # is too near the top of the book for a 5% gain to exist.
                self._watching[raw.topic_id] = (
                    raw.end_ms, "no bracket fits around the price on offer")
                continue
            tp_price, stop_price = bracket

            self._watching.pop(raw.topic_id, None)
            quoted_stake = stake
            placed = self._place_leg(raw, side, price, stake, quote)
            if placed is None:
                continue                  # killed by the venue; nothing open
            price, stake, order_id = placed
            # The shares the buy actually returned. The fee comes out of
            # them, so cost / price overstates the holding, and every exit
            # sized from that was refused as exceeding the shares available.
            # Scaled down if the confirmed fill came back smaller than quoted.
            shares = (quote.amount_out * min(1.0, stake / quoted_stake)
                      if quote is not None and quoted_stake > 0 else None)
            # Re-derive from the CONFIRMED fill. _place_leg can come back
            # with a different price and a smaller stake than the screen, and
            # a bracket around the wrong price is the one failure this whole
            # strategy cannot absorb.
            bracket = bracket_prices(price, raw.fee_bps,
                                     cfg.scalp_take_profit_pct,
                                     cfg.scalp_stop_loss_pct)
            if bracket is None:
                LOG.error("%s: filled at %.4f, which no bracket fits; "
                          "closing it straight back out", raw.slug, price)
                tp_price = stop_price = 0.0
            else:
                tp_price, stop_price = bracket

            # model_prob is the MARKET'S implied probability, not a forecast:
            # this path makes none. Recorded so the journal row says what was
            # paid. edge is 0.0 because paying the market price is by
            # definition no edge over it, and every one of these rows closes
            # as settle_source='sold', which diagnose() already keeps out of
            # the calibration buckets.
            sig = Signal(side, model_prob=breakeven_probability(price,
                                                               raw.fee_bps),
                         fill_price=price, edge=0.0, stake_usdt=stake,
                         seconds_left=secs)
            tid = self._journal.record(mode, raw, sig, spot=math.nan,
                                       sigma=math.nan, bankroll=bankroll,
                                       order_id=order_id,
                                       order_type=OrderType.MARKET.value)
            key = (raw.symbol, side)
            self._positions[key] = Position(tid, raw, sig, stake, 1,
                                            shares=shares)
            self._scalp_entries[raw.topic_id] = (raw.end_ms, taken + 1)
            self._scalp_last_entry[raw.symbol] = now_s
            LOG.info("SCALP %s %s | in %.4f (%.2f) tp %.4f stop %.4f | perp "
                     "%+.1fbp basis %+.1fbp (%.0fs left, #%d)",
                     raw.slug, side.value, price, stake, tp_price, stop_price,
                     move_bps, dislocation, secs, taken + 1)

            if bracket is None:
                self._sell_now(self._positions[key], "no bracket fits")
            else:
                self._arm_bracket(key, price, tp_price, stop_price)

            if (len(self._positions) >= cfg.max_concurrent_positions
                    or self._available(bankroll) < cfg.min_stake_usdt):
                return

    def _arm_bracket(self, key: tuple[str, Side], entry: float,
                     tp_price: float, stop_price: float) -> None:
        """
        Post the resting take-profit and arm the stop.

        Only the take-profit becomes an order. The stop is a price this bot
        watches, because a SELL limit below the bid is marketable and would
        close the position on the spot instead of waiting -- see Bracket.

        A take-profit the venue refuses is not fatal and is not silent: the
        stop still guards the position, and the flatten deadline still closes
        it. What would be fatal is recording a bracket whose take-profit does
        not exist, so the order id stays None and _check_stops has nothing to
        cancel.
        """
        pos = self._positions.get(key)
        if pos is None:
            return
        tp_price = pos.rnd.round_price(tp_price)
        shares = pos.held_shares
        order_id: str | None = None
        if 0.0 < tp_price < 1.0 and shares > EPS:
            plan = OrderPlan(side=pos.signal.side, action=Action.SELL,
                             order_type=OrderType.LIMIT, amount=shares,
                             price_limit=tp_price)
            try:
                if self._live:
                    quote = self._client.get_quote(pos.rnd, plan)
                    order_id = str(self._client.place_order(pos.rnd, quote))
                else:
                    order_id = str(self._paper_book.place(plan, pos.rnd))
            except (ApiError, requests.RequestException) as exc:
                LOG.error("%s: the take-profit was refused (%s); the stop and "
                          "the flatten deadline are all that guard this "
                          "position", pos.rnd.slug, exc)
                order_id = None
            if order_id is not None:
                self._pending[order_id] = PendingOrder(
                    order_id=order_id, rnd=pos.rnd, plan=plan,
                    signal=pos.signal,
                    # Round end, not the entry window: a take-profit is not
                    # an entry and has no reason to stop being useful when
                    # entries do. The flatten deadline retracts it first in
                    # the ordinary case; this is the backstop.
                    expires_at_ms=pos.rnd.end_ms,
                    filled_usdt=0.0, filled_shares=0.0,
                    trade_id=pos.trade_id)
        self._brackets[key] = Bracket(entry_price=entry, tp_price=tp_price,
                                      stop_price=stop_price,
                                      tp_order_id=order_id)

    def _check_stops(self) -> None:
        """
        Fire the stop leg on any bracket whose bid has fallen to it.

        Runs after the reaper, so a take-profit that filled this pass has
        already closed its position and taken its bracket with it -- which is
        the "cancel the other one" half of the pair, in the direction where
        there is nothing to cancel.
        """
        if not self._cfg.scalp:
            return
        for key, bracket in list(self._brackets.items()):
            pos = self._positions.get(key)
            if pos is None:
                self._brackets.pop(key, None)
                continue
            bids = self._market_data.bids(pos.rnd, pos.signal.side)
            if not bids:
                continue
            bid = bids[0][0]
            if bid > bracket.stop_price:
                continue
            LOG.info("STOP %s %s | bid %.4f at or under %.4f (entry %.4f, "
                     "take-profit was %.4f)", pos.rnd.slug,
                     pos.signal.side.value, bid, bracket.stop_price,
                     bracket.entry_price, bracket.tp_price)
            if bracket.tp_order_id and not self._retract(
                    bracket.tp_order_id, "stop triggered"):
                # The take-profit could not be reached. Selling now would put
                # more shares on offer than the position holds, and the venue
                # would fill both. Wait a pass; the stop re-fires while the
                # bid stays down.
                continue
            self._brackets.pop(key, None)
            pos = self._positions.get(key)
            if pos is None:
                # The cancel raced the take-profit and lost: it had already
                # filled, and _retract booked it. That is the good ending.
                continue
            self._sell_now(pos, "stop")

    def _flatten_scalps(self) -> None:
        """
        Close everything before the last minute, and place nothing inside it.

        The book thins as a round ends, which is the whole reason the profile
        stops early -- so this fires at the edge of that minute rather than
        inside it, and each round is flattened exactly once. Re-running it
        would cancel the very exit order the first pass placed.
        """
        if not self._cfg.scalp:
            return
        now_ms = self._client.now_ms()
        due = {p.rnd.topic_id for p in self._positions.values()
               if p.rnd.seconds_remaining(now_ms) <= self._cfg.scalp_flatten_s}
        due -= self._flattened
        if not due:
            return
        for order_id, pending in list(self._pending.items()):
            if pending.rnd.topic_id in due:
                self._retract(order_id, "flatten deadline")
        for key, pos in list(self._positions.items()):
            if pos.rnd.topic_id not in due:
                continue
            self._brackets.pop(key, None)
            if pos.committed_usdt < DUST_USDT:
                LOG.info("%s: %.4f USDT left is too small to sell; it settles "
                         "with the round", pos.rnd.slug, pos.committed_usdt)
                continue
            LOG.info("FLATTEN %s %s | %.4f USDT with %.0fs left",
                     pos.rnd.slug, pos.signal.side.value, pos.committed_usdt,
                     pos.rnd.seconds_remaining(now_ms))
            if not self._sell_now(pos, "flatten"):
                # The honest fallback. Retrying into a book that is not there
                # is how a 5% loss becomes a 100% one; letting the oracle
                # decide is the smaller of the two bad endings, and it is the
                # one the rest of this bot already knows how to finish.
                LOG.error("%s: could not flatten %.4f USDT; it will run to "
                          "settlement as a FULL-STAKE bet, which is not what "
                          "this profile's risk numbers assume",
                          pos.rnd.slug, pos.committed_usdt)
        self._flattened |= due
