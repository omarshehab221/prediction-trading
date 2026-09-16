"""
The hybrid profile: a locked straddle when a round offers one, a buffer
entry when it does not, and a stop under anything that can still lose.
"""

from __future__ import annotations


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
