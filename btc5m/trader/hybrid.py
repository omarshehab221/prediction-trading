"""
The hybrid profile: a locked straddle when a round offers one, a buffer
entry when it does not, and a stop under anything that can still lose.
"""

from __future__ import annotations

import math

from btc5m.domain import Bracket


class HybridMixin:

    def _maybe_enter_hybrid(self, bankroll: float, mode: str) -> None:
        """
        Straddle first, then the model -- in that order, every pass.

        Order is the whole priority rule. The straddle path completes open
        legs and opens new ones; anything it opens is a position on its
        symbol, and the model path already refuses a symbol with a position,
        so a market holds one strategy's bet per round and never both. Both
        paths size against _available, which counts what the straddle just
        committed, so the model cannot spend the same money twice.
        """
        self._maybe_enter_straddle(bankroll, mode)
        self._maybe_enter_model(bankroll, mode)

    def _sync_hybrid_stops(self) -> None:
        """
        Make the brackets say what the positions are, every pass.

        Declarative rather than armed at each entry point, because four
        paths change what a stop should be -- a buffer entry, a first
        straddle leg, its completion, and a top-up -- and arming at each is
        four places for one to be forgotten. Here there is one rule:
        anything that can still lose carries a stop; a locked pair does not.
        """
        if not self._cfg.hybrid:
            return
        held = {pos.trade_id for pos in self._positions.values()}
        self._hybrid_stopped &= held
        sides_by_symbol: dict[str, int] = {}
        for symbol, _side in self._positions:
            sides_by_symbol[symbol] = sides_by_symbol.get(symbol, 0) + 1
        for key, pos in self._positions.items():
            if sides_by_symbol[key[0]] > 1:
                # Both sides held: the payout is locked whichever way the
                # round settles, and selling either leg would unlock it.
                self._brackets.pop(key, None)
                continue
            if pos.trade_id in self._hybrid_stopped:
                continue
            entry = pos.signal.fill_price
            current = self._brackets.get(key)
            if current is not None and current.entry_price == entry:
                continue
            stop = pos.rnd.round_price(
                entry * (1.0 - self._cfg.hybrid_stop_loss_pct))
            # No take-profit: winners ride to settlement. An infinite target
            # is one _check_stops' ">=" can never reach.
            self._brackets[key] = Bracket(entry_price=entry, tp_price=math.inf,
                                          stop_price=stop)
