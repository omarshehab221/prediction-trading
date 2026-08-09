#!/usr/bin/env python3
"""
patterns.py -- does any kline pattern actually predict the next 5 minutes?

MOTIVATION
----------
On a 5m contract, even a 2 bps directional forecast moves P(up) to ~55%, which
is worth real money. So the question deserves a measurement rather than an
opinion. This harness tests candlestick patterns and order-flow features
against future returns with the discipline that makes the answer trustworthy:

  * chronological train/test split -- no shuffling a time series
  * out-of-sample confirmation, since in-sample hit rates are always flattering
  * Benjamini-Hochberg correction, because testing 30 features at p<0.05
    produces roughly 1-2 "discoveries" from pure noise every single time
  * a shuffled-label null run, printed alongside, so you can see what chance
    alone looks like on this data

WHAT IT DOES NOT DO
-------------------
It does not wire anything into the bot. A feature that survives here is a
candidate, not a strategy: the bot's calibration report is still the arbiter.

    python3 patterns.py --days 30
    python3 patterns.py --selftest      # verify the harness itself

Requires: requests (only for --days; --selftest is offline).
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Configured at runtime by main(); these are defaults for the self-test.
INTERVAL = "5m"
HORIZON = 1              # candles ahead
OUTCOME_MODE = "close_vs_open"
TRAIN_FRACTION = 0.6


@dataclass
class Candle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    taker_buy_base: float

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def range_(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def taker_buy_ratio(self) -> float:
        """Share of volume from aggressive buyers -- an order-flow proxy."""
        return self.taker_buy_base / self.volume if self.volume > 0 else 0.5


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


def interval_minutes(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 1, "h": 60, "d": 1440}[unit]


def fetch_klines(days: int, symbol: str = "BTCUSDT",
                 interval: str = "5m") -> list[Candle]:
    """Download klines from Binance's public endpoint."""
    import requests

    needed = days * 24 * 60 // interval_minutes(interval)
    out: list[Candle] = []
    end = int(time.time() * 1000)
    session = requests.Session()

    while len(out) < needed:
        r = session.get("https://api.binance.com/api/v3/klines",
                        params={"symbol": symbol, "interval": interval,
                                "endTime": end, "limit": 1000}, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for k in rows:
            out.append(Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                              float(k[4]), float(k[5]), int(k[8]),
                              float(k[9])))
        end = int(rows[0][0]) - 1
        print(f"\r  fetched {len(out)} candles", end="", file=sys.stderr)
        time.sleep(0.15)

    print(file=sys.stderr)
    out.sort(key=lambda c: c.open_ms)
    return out


def synthetic(n: int, seed: int, planted_edge: float = 0.0) -> list[Candle]:
    """
    Random-walk candles, optionally with a planted signal.

    `planted_edge` makes a high taker-buy ratio predictive of the NEXT
    candle's close-vs-open, which is what the contract settles on. Planting it
    into close-vs-close instead would let the harness pass a test the real
    instrument never asks.
    """
    rng = random.Random(seed)
    out: list[Candle] = []
    price = 65_000.0
    pending_drift = 0.0
    for i in range(n):
        o = price
        drift = pending_drift
        ret = rng.gauss(drift, 0.0004)
        c = o * math.exp(ret)
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.0002)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.0002)))
        vol = abs(rng.gauss(100, 30)) + 1
        tbr = rng.uniform(0.3, 0.7)
        # Plant the effect: this candle's buy ratio drifts the NEXT candle.
        pending_drift = (tbr - 0.5) * planted_edge
        out.append(Candle(i * 60_000, o, hi, lo, c, vol,
                          int(vol), vol * tbr))
        price = c
    return out


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------

Feature = Callable[[list[Candle], int], Optional[bool]]


def _eps(c: Candle) -> float:
    return max(c.range_, 1e-9)


def build_features() -> dict[str, Feature]:
    """
    Each feature returns True (predict up), False (predict down) or None
    (no opinion at this bar).
    """
    f: dict[str, Feature] = {}

    # --- classic candlestick patterns --------------------------------------
    def bullish_engulfing(k, i):
        if i < 1:
            return None
        a, b = k[i - 1], k[i]
        if not (not a.bullish and b.bullish):
            return None
        return True if (b.close > a.open and b.open < a.close) else None
    f["bullish_engulfing"] = bullish_engulfing

    def bearish_engulfing(k, i):
        if i < 1:
            return None
        a, b = k[i - 1], k[i]
        if not (a.bullish and not b.bullish):
            return None
        return False if (b.close < a.open and b.open > a.close) else None
    f["bearish_engulfing"] = bearish_engulfing

    def hammer(k, i):
        c = k[i]
        return True if (c.lower_wick > 2 * abs(c.body)
                        and c.upper_wick < abs(c.body)) else None
    f["hammer"] = hammer

    def shooting_star(k, i):
        c = k[i]
        return False if (c.upper_wick > 2 * abs(c.body)
                         and c.lower_wick < abs(c.body)) else None
    f["shooting_star"] = shooting_star

    def doji(k, i):
        c = k[i]
        return None if abs(c.body) > 0.1 * _eps(c) else True
    f["doji_up"] = doji

    def marubozu_up(k, i):
        c = k[i]
        return True if (c.bullish and abs(c.body) > 0.9 * _eps(c)) else None
    f["marubozu_up"] = marubozu_up

    def marubozu_down(k, i):
        c = k[i]
        return False if (not c.bullish and abs(c.body) > 0.9 * _eps(c)) else None
    f["marubozu_down"] = marubozu_down

    def three_soldiers(k, i):
        if i < 2:
            return None
        return True if all(k[i - j].bullish for j in range(3)) else None
    f["three_white_soldiers"] = three_soldiers

    def three_crows(k, i):
        if i < 2:
            return None
        return False if all(not k[i - j].bullish for j in range(3)) else None
    f["three_black_crows"] = three_crows

    # --- momentum and reversal --------------------------------------------
    # Only momentum is registered. `reversal_Nm` was exactly NOT
    # `momentum_Nm` -- the same coin flip counted twice, which both inflated
    # the family size and guaranteed that if one "passed", its mirror did too.
    # A hit rate below 50% already reports the reversal case.
    for lookback in (1, 3, 5, 15):
        def momentum(k, i, n=lookback):
            if i < n:
                return None
            return k[i].close > k[i - n].close
        f[f"momentum_{lookback}m"] = momentum

    # --- order flow (the class with the best documented track record) ------
    def taker_pressure(k, i):
        r = k[i].taker_buy_ratio
        if 0.45 <= r <= 0.55:
            return None
        return r > 0.55
    f["taker_buy_pressure"] = taker_pressure

    def taker_pressure_strong(k, i):
        r = k[i].taker_buy_ratio
        if 0.35 <= r <= 0.65:
            return None
        return r > 0.65
    f["taker_buy_pressure_strong"] = taker_pressure_strong

    def taker_pressure_3m(k, i):
        if i < 2:
            return None
        window = k[i - 2:i + 1]
        vol = sum(c.volume for c in window)
        if vol <= 0:
            return None
        r = sum(c.taker_buy_base for c in window) / vol
        if 0.45 <= r <= 0.55:
            return None
        return r > 0.55
    f["taker_buy_pressure_3m"] = taker_pressure_3m

    # --- volume and volatility --------------------------------------------
    def volume_spike_direction(k, i):
        if i < 20:
            return None
        avg = statistics.fmean(c.volume for c in k[i - 20:i])
        if avg <= 0 or k[i].volume < 3 * avg:
            return None
        return k[i].bullish
    f["volume_spike_direction"] = volume_spike_direction

    def narrow_range_breakout(k, i):
        if i < 20:
            return None
        avg = statistics.fmean(c.range_ for c in k[i - 20:i])
        if avg <= 0 or k[i].range_ > 0.5 * avg:
            return None
        return k[i].bullish
    f["narrow_range"] = narrow_range_breakout

    def trade_count_surge(k, i):
        if i < 20:
            return None
        avg = statistics.fmean(c.trades for c in k[i - 20:i])
        if avg <= 0 or k[i].trades < 2 * avg:
            return None
        return k[i].bullish
    f["trade_count_surge"] = trade_count_surge

    return f


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    train_n: int
    train_hit: float
    test_n: int
    test_hit: float
    p_value: float
    mean_bps: float


def outcome(candles: list[Candle], i: int) -> Optional[bool]:
    """
    Did the target candle finish up?

    `close_vs_open` compares the target candle's close to its OWN open, which
    is exactly how the contract settles: the strike is the price at the round
    boundary, i.e. that candle's open. On 5m candles aligned to the same
    boundaries the two are the same event.

    `close_vs_close` is the looser "is price higher N bars later" framing.
    """
    j = i + HORIZON
    if j >= len(candles):
        return None
    target = candles[j]
    reference = target.open if OUTCOME_MODE == "close_vs_open" \
        else candles[i].close
    if target.close == reference:
        return None                      # exact tie: no information
    return target.close > reference


def forward_bps(candles: list[Candle], i: int) -> Optional[float]:
    j = i + HORIZON
    if j >= len(candles):
        return None
    target = candles[j]
    reference = target.open if OUTCOME_MODE == "close_vs_open" \
        else candles[i].close
    if reference <= 0:
        return None
    return (target.close / reference - 1.0) * 1e4


def binomial_p(hits: int, n: int, overlap: Optional[int] = None) -> float:
    """
    Two-sided p-value for a hit rate against a fair coin.

    Consecutive 1m bars share the same 5m forward window, so observations are
    NOT independent and a plain binomial test is anti-conservative: it read a
    49.1% hit rate on a pure random walk as significant at p=0.023. Dividing
    by the overlap gives the effective sample size, which is the standard
    conservative correction and takes that same case to p=0.31.
    """
    if n < 10:
        return 1.0
    if overlap is None:
        # Windows overlap only when the horizon spans more than one candle.
        # At horizon 1 each observation is a distinct candle, so no
        # correction applies and the full sample counts.
        overlap = HORIZON
    effective = max(n / max(overlap, 1), 1.0)
    rate = hits / n
    z = (rate - 0.5) * math.sqrt(4 * effective)
    return math.erfc(abs(z) / math.sqrt(2))


def evaluate(candles: list[Candle], features: dict[str, Feature],
             shuffle_labels: bool = False, seed: int = 0) -> list[Result]:
    split = int(len(candles) * TRAIN_FRACTION)
    rng = random.Random(seed)
    results: list[Result] = []

    for name, fn in features.items():
        train_hits = train_n = 0
        test_hits = test_n = 0
        moves: list[float] = []

        for i in range(len(candles) - HORIZON):
            try:
                signal = fn(candles, i)
            except (IndexError, ZeroDivisionError, ValueError):
                continue
            if signal is None:
                continue
            actual = outcome(candles, i)
            if actual is None:
                continue
            if shuffle_labels:
                actual = rng.random() < 0.5
            correct = (signal == actual)

            if i < split:
                train_n += 1
                train_hits += correct
            else:
                test_n += 1
                test_hits += correct
                bps = forward_bps(candles, i)
                if bps is not None:
                    moves.append(bps if signal else -bps)

        if train_n < 30 or test_n < 30:
            continue
        results.append(Result(
            name, train_n, train_hits / train_n, test_n, test_hits / test_n,
            binomial_p(test_hits, test_n),
            statistics.fmean(moves) if moves else 0.0))

    return sorted(results, key=lambda r: r.p_value)


def benjamini_hochberg(results: list[Result], alpha: float = 0.05) -> set[str]:
    """Names surviving FDR control at `alpha`."""
    if not results:
        return set()
    ordered = sorted(results, key=lambda r: r.p_value)
    m = len(ordered)
    survivors: set[str] = set()
    for rank, r in enumerate(ordered, 1):
        if r.p_value <= alpha * rank / m:
            survivors = {x.name for x in ordered[:rank]}
    return survivors


def report(candles: list[Candle], label: str, seed: int = 0) -> set[str]:
    features = build_features()
    real = evaluate(candles, features, seed=seed)
    null = evaluate(candles, features, shuffle_labels=True, seed=seed + 1)
    survivors = benjamini_hochberg(real)
    null_survivors = benjamini_hochberg(null)

    print(f"\n=== {label} ===")
    print(f"  {len(candles)} x {INTERVAL} candles, horizon {HORIZON} candle(s), "
          f"outcome {OUTCOME_MODE}")
    print(f"  {TRAIN_FRACTION:.0%} train / {1-TRAIN_FRACTION:.0%} test; "
          f"p-values use effective n = n/{HORIZON} for window overlap\n")
    print(f"  {'feature':<26} {'train':>7} {'test':>7} {'n':>6} {'n_eff':>6} "
          f"{'p':>8} {'bps':>7}")
    for r in real:
        mark = "  <-- survives FDR" if r.name in survivors else ""
        print(f"  {r.name:<26} {r.train_hit:>6.1%} {r.test_hit:>6.1%} "
              f"{r.test_n:>6} {max(r.test_n // HORIZON, 1):>6} "
              f"{r.p_value:>8.4f} {r.mean_bps:>+7.2f}{mark}")

    print(f"\n  survive FDR(5%) on real labels    : "
          f"{sorted(survivors) if survivors else 'none'}")
    print(f"  survive FDR(5%) on SHUFFLED labels: "
          f"{sorted(null_survivors) if null_survivors else 'none'}")
    if null_survivors:
        print("  ^ the null run found 'signal' in noise; treat real "
              "survivors with matching scepticism")
    return survivors


def selftest() -> int:
    """The harness must find a planted edge and must not invent one."""
    print(f"SELF-TEST ({INTERVAL} candles, horizon {HORIZON}, "
          f"{OUTCOME_MODE}): can the harness tell signal from noise?")

    noise = synthetic(40_000, seed=1, planted_edge=0.0)
    found_in_noise = report(noise, "PURE NOISE (expect: nothing)", seed=5)

    planted = synthetic(40_000, seed=2, planted_edge=0.004)
    found_in_planted = report(
        planted, "PLANTED taker-flow EDGE (expect: taker features)", seed=5)

    print("\n=== SELF-TEST VERDICT ===")
    ok = True
    if found_in_noise:
        print(f"  FAIL: reported {sorted(found_in_noise)} on pure noise")
        ok = False
    else:
        print("  PASS: found nothing in pure noise")
    if any("taker" in n for n in found_in_planted):
        print("  PASS: recovered the planted order-flow edge")
    else:
        print(f"  FAIL: missed the planted edge (found {sorted(found_in_planted)})")
        ok = False
    return 0 if ok else 1


def main() -> int:
    global INTERVAL, HORIZON, OUTCOME_MODE

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="5m",
                    help="candle size; 5m matches the contract's own rounds")
    ap.add_argument("--horizon", type=int, default=1,
                    help="candles ahead to predict (1 = the next round)")
    ap.add_argument("--outcome", default="close_vs_open",
                    choices=("close_vs_open", "close_vs_close"),
                    help="close_vs_open matches how the contract settles")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    INTERVAL, HORIZON, OUTCOME_MODE = args.interval, args.horizon, args.outcome

    if args.selftest:
        return selftest()

    if args.interval == "5m" and args.horizon == 1:
        print("Note: 5m rounds start on 5-minute boundaries, so a 5m candle IS")
        print("a round. close_vs_open on the next candle is exactly the bet.\n")

    candles = fetch_klines(args.days, args.symbol, args.interval)
    minimum = 2000
    if len(candles) < minimum:
        print(f"Only {len(candles)} candles; need >= {minimum}. "
              f"At {args.interval} that is roughly "
              f"{minimum * interval_minutes(args.interval) // 1440} days.",
              file=sys.stderr)
        return 1
    survivors = report(
        candles, f"{args.symbol} {args.interval} last {args.days} day(s)")

    print("\n=== WHAT TO DO WITH THIS ===")
    if not survivors:
        print("  Nothing survived correction. That is the usual result for")
        print("  candlestick patterns on 1m data, and it is a real answer:")
        print("  do not wire any of these into the bot.")
    else:
        print(f"  {sorted(survivors)} survived out-of-sample with FDR control.")
        print("  That is a CANDIDATE, not a strategy. Before trusting it:")
        print("   - re-run on a different date range; edges that vanish were")
        print("     period-specific, not real")
        print("   - check the bps column: a positive hit rate with a negative")
        print("     mean move loses money despite 'winning' more often")
        print("   - run it in the bot's paper mode and read the calibration")
        print("     report, which is the only test that includes fill prices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
