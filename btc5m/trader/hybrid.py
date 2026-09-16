"""
The hybrid profile: a locked straddle when a round offers one, a buffer
entry when it does not, and a stop under every buffer position.
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

        Declarative rather than armed at each entry point, because a buffer
        entry and each of its top-ups change what a stop should be, and
        arming at each is several places for one to be forgotten. The rule:
        a buffer position carries a stop; a straddle leg never does.

        Straddle legs were stopped too, until the 2026-09-16 shadow session:
        nine unpaired first legs were stopped 20-60s after entry for -5.33
        USDT while the two pairs that completed made +1.43. A cheap first
        leg falling is the strategy waiting for its partner, not failing,
        and selling it forecloses the completion. A leg that never finds
        its partner rides to settlement, as it does in the straddle profile.
        """
        if not self._cfg.hybrid:
            return
        held = {pos.trade_id for pos in self._positions.values()}
        self._hybrid_stopped &= held
        self._hybrid_straddle_legs &= held
        for key, pos in self._positions.items():
            if pos.trade_id in self._hybrid_straddle_legs:
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
