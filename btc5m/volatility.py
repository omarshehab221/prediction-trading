"""
How volatile the market is, how fat its tails are, and which way it
has been moving.
"""

from __future__ import annotations

import itertools
import math
import statistics
import time

from btc5m.config import Config
from btc5m.constants import LOG
from btc5m.domain import Trend
from btc5m.errors import ApiError

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config_file import ConfigStore

def _projected_rounds(impulse: float, decay: float,
                      floor: float) -> float:
    """
    How many more rounds a decaying move stays above the noise floor.

    Geometric decay: the size after n rounds is impulse * decay**n, and the
    move stops being tradable once it drops under `floor`. Solving for n
    gives the answer in
    the units the decision is actually made in -- rounds, not ratios.

    A move that is holding or growing (decay >= 1) is not decaying at all
    and gets a large finite number rather than infinity, so callers can
    compare it without special-casing.
    """
    if impulse <= 0 or floor <= 0:
        return 0.0
    if impulse < floor:
        return 0.0                      # already under the floor
    if decay >= 1.0:
        return 99.0                     # holding or accelerating
    if decay <= 0.0:
        return 0.0                      # reversed outright
    return math.log(floor / impulse) / math.log(decay)


class VolatilityEstimator:
    """Annualised sigma AND tail thickness from recent 1m returns."""

    def __init__(self, cfg: Config | ConfigStore, market_data) -> None:
        self._store = None if isinstance(cfg, Config) else cfg
        self._static_cfg = cfg if isinstance(cfg, Config) else None
        self._market_data = market_data
        self._cache: dict[str, tuple[float, float]] = {}
        self._df_cache: dict[str, float | None] = {}
        self._clamped: dict[str, bool] = {}
        self._raw: dict[str, float] = {}
        self._trend: dict[str, Trend] = {}

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    def sigma_annual(self, symbol: str | None = None) -> float:
        symbol = symbol or self._cfg.symbol
        cached = self._cache.get(symbol)
        if cached is not None and time.time() - cached[1] < self._cfg.vol_cache_s:
            return cached[0]

        # Through the seam rather than fetched here: the socket keeps a
        # window that is already current, and the REST fetch behind it is
        # the same one this used to make.
        closes = self._market_data.closes(symbol)
        if len(closes) < 10:
            raise ApiError("insufficient kline history for volatility")

        # Guard BOTH endpoints: a single malformed close (0 or negative) would
        # otherwise raise a math domain error and take the whole loop down.
        rets = [math.log(b / a) for a, b in itertools.pairwise(closes)
                if a > 0 and b > 0]
        if len(rets) < 10:
            raise ApiError("insufficient valid returns for volatility")
        # Recent window for sigma (volatility drifts); full window for tails.
        recent = rets[-self._cfg.sigma_window_min:]
        raw = statistics.pstdev(recent) * math.sqrt(365.0 * 24.0 * 60.0)
        annual = max(self._cfg.vol_floor_annual,
                     min(self._cfg.vol_ceiling_annual, raw))
        self._raw[symbol] = raw
        self._clamped[symbol] = abs(annual - raw) > 1e-12
        if self._clamped[symbol]:
            LOG.warning("Volatility %.4f clamped to %.4f for %s -- the model "
                        "is no longer measuring the market", raw, annual, symbol)

        self._df_cache[symbol] = self._estimate_df(rets, statistics.pstdev(rets))
        # Measured from the same closes rather than a second request: a trend
        # read off a different fetch than the sigma it is compared against is
        # two snapshots of two moments pretending to be one.
        self._trend[symbol] = self._measure_trend(closes)
        self._cache[symbol] = (annual, time.time())
        return annual

    def _measure_trend(self, closes: list[float]) -> Trend:
        """
        Direction, thrust, straightness and remaining life of the move.

        Everything comes from the closes already in hand. The series is cut
        into round-length blocks ending at NOW, so the last block is the move
        currently in progress -- that block, not the count of finished ones,
        is what says a trend is starting.

        Direction is taken from that last block too. When it disagrees with
        the blocks before it, this is a reversal and the run resets to one:
        the correct reading of a fresh reversal is "a new trend beginning",
        not "the old trend continuing", and a detector anchored to the older
        blocks would call the top of a move a buy.
        """
        cfg = self._cfg
        if not cfg.trend_follow:
            return Trend()
        window = [c for c in closes[-cfg.trend_lookback_min:] if c > 0]
        if len(window) < 10:
            return Trend()

        steps = [math.log(b / a) for a, b in itertools.pairwise(window)]
        sd_step = statistics.pstdev(steps)
        block = max(1, round(cfg.round_seconds / 60.0))
        if sd_step <= 0 or len(steps) < block:
            return Trend()

        # Blocks of one round each, oldest first, the last ending at now.
        edges = list(range(len(window) - 1, -1, -block))[::-1]
        if len(edges) < 2:
            return Trend()
        blocks = [math.log(window[b] / window[a])
                  for a, b in itertools.pairwise(edges)]

        current = blocks[-1]
        if current == 0.0:
            return Trend()
        direction = 1 if current > 0 else -1

        # One block of pure noise, as the yardstick every size is measured
        # against. Without it "a big move" would mean a fixed number of basis
        # points, which is a different thing in a calm hour than a wild one.
        sigma_block = sd_step * math.sqrt(block)
        impulse = abs(current) / sigma_block

        run = 0
        for value in reversed(blocks):
            if value == 0 or (value > 0) != (direction > 0):
                break
            run += 1

        # Straightness and strength are measured over the RUN, not over the
        # whole lookback: including blocks that moved the other way describes
        # a market that reversed, not the move being traded.
        span = min(run * block, len(window) - 1)
        segment = window[-(span + 1):]
        net = math.log(segment[-1] / segment[0])
        seg_steps = [math.log(b / a) for a, b in itertools.pairwise(segment)]
        travelled = sum(abs(s) for s in seg_steps)
        efficiency = abs(net) / travelled if travelled > 0 else 0.0
        z = (abs(net) / (sd_step * math.sqrt(len(seg_steps)))
             if seg_steps else 0.0)

        # Decay, and what it implies about how much life is left. A move
        # shedding half its size each block has nothing left for the round about
        # to start, and that is the round the entry would be taken in.
        decay = 1.0
        if run >= 2 and abs(blocks[-2]) > 0:
            decay = abs(current) / abs(blocks[-2])
        rounds_left = _projected_rounds(impulse, decay,
                                        cfg.trend_min_impulse)

        # Order matters. "Nothing is happening" and "something was happening
        # and has died" are different findings, and only the second is a
        # warning. A weak block with a run behind it is the tail of a move,
        # not the absence of one, so it is labelled fading rather than none.
        if impulse < cfg.trend_min_impulse and run <= 1:
            phase = "none"
        elif (impulse < cfg.trend_min_impulse
                or (run >= 2 and decay < cfg.trend_decay_floor)
                or rounds_left < 1.0):
            phase = "fading"
        elif run <= 1:
            phase = "building"
        else:
            phase = "running"

        return Trend(direction=direction, impulse=impulse, z=z,
                     efficiency=efficiency, run=run, decay=decay,
                     rounds_left=rounds_left, phase=phase)

    def trend(self, symbol: str | None = None) -> Trend:
        """Trend state for `symbol`. Call sigma_annual first."""
        return self._trend.get(symbol or self._cfg.symbol, Trend())

    def _estimate_df(self, rets: list[float], sd: float) -> float | None:
        """
        Degrees of freedom implied by realised excess kurtosis.

        For a Student-t, excess kurtosis = 6 / (df - 4), so df = 4 + 6/k.

        The sample estimator has standard error ~sqrt(24/n), so a fixed small
        cutoff would flag ordinary sampling noise as fat tails -- a 500-point
        Gaussian draw routinely shows +0.1 excess kurtosis. Require two
        standard errors of evidence instead, and refuse to guess at all below
        200 samples. Returns None when the data does not look convincingly
        heavy-tailed, in which case pricing stays Gaussian.
        """
        n = len(rets)
        if not self._cfg.use_fat_tails or sd <= 0 or n < 200:
            return None
        mu = statistics.fmean(rets)
        z4 = statistics.fmean([((x - mu) / sd) ** 4 for x in rets])
        excess = z4 - 3.0
        if excess <= 2.0 * math.sqrt(24.0 / n):
            return None
        df = 4.0 + 6.0 / excess
        return max(self._cfg.tail_df_floor,
                   min(self._cfg.tail_df_ceiling, df))

    def tail_df(self, symbol: str | None = None) -> float | None:
        """Tail parameter for `symbol`. Call sigma_annual first."""
        return self._df_cache.get(symbol or self._cfg.symbol)

    def raw_sigma(self, symbol: str | None = None) -> float | None:
        """Measured sigma before clamping, for diagnostics."""
        return self._raw.get(symbol or self._cfg.symbol)

    def is_clamped(self, symbol: str | None = None) -> bool:
        """True if the last sigma hit a bound and is therefore not a measurement."""
        return self._clamped.get(symbol or self._cfg.symbol, False)
