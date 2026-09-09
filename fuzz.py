#!/usr/bin/env python3
"""
fuzz.py -- property-based testing with hostile inputs.

Unit tests check cases I thought of. This asserts INVARIANTS over randomly
generated inputs, including adversarial ones (NaN, infinity, zero, negatives,
enormous and denormal magnitudes, malformed API payloads). It finds cases I
would not have thought to write.

    python3 fuzz.py [--trials 20000] [--seed N]

Exit code 0 only when every invariant holds on every trial.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from decimal import Decimal

import btc_5m_predictor as m
import ws_feeds

HOSTILE_FLOATS = [
    0.0, -0.0, 1.0, -1.0, 1e-300, 1e300, -1e300,
    float("inf"), float("-inf"), float("nan"),
    sys.float_info.min, sys.float_info.max, sys.float_info.epsilon,
]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{name}: {detail}")


def cfg(**kw) -> m.Config:
    base = dict(api_key="k", api_secret="s")
    base.update(kw)
    return m.Config(**base)


# --------------------------------------------------------------------------
# Pure numeric invariants
# --------------------------------------------------------------------------


def fuzz_digital(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        spot = rng.choice([rng.uniform(1e-6, 1e9), rng.uniform(5e4, 1.5e5)])
        strike = rng.choice([rng.uniform(1e-6, 1e9), spot * rng.uniform(.9, 1.1)])
        sigma = rng.uniform(1e-6, 50.0)
        secs = rng.choice([0.0, rng.uniform(0, 1e6), 1e-9])
        df = rng.choice([None, 2.0001, 2.5, 4.0, 30.0, 1e6])
        try:
            p = m.digital_up_probability(spot, strike, sigma, secs, df)
        except ValueError:
            continue                       # documented rejection
        check("digital range", 0.0 <= p <= 1.0, f"{p} spot={spot} k={strike}")
        check("digital finite", math.isfinite(p), str(p))

    # Monotonicity: higher spot can never lower P(UP).
    for _ in range(trials // 4):
        strike, sigma = 100_000.0, rng.uniform(0.05, 3.0)
        secs = rng.uniform(1, 300)
        df = rng.choice([None, 3.0, 6.0])
        a, b = sorted(rng.uniform(9e4, 1.1e5) for _ in range(2))
        pa = m.digital_up_probability(a, strike, sigma, secs, df)
        pb = m.digital_up_probability(b, strike, sigma, secs, df)
        check("digital monotonic in spot", pb >= pa - 1e-12,
              f"{a}->{pa}  {b}->{pb}")

    # Symmetry: P(UP | S,K) + P(UP | K,S) == 1.
    for _ in range(trials // 4):
        s_, k_ = rng.uniform(9e4, 1.1e5), rng.uniform(9e4, 1.1e5)
        sigma, secs = rng.uniform(0.1, 2.0), rng.uniform(1, 300)
        df = rng.choice([None, 4.0])
        total = (m.digital_up_probability(s_, k_, sigma, secs, df)
                 + m.digital_up_probability(k_, s_, sigma, secs, df))
        check("digital symmetry", abs(total - 1.0) < 1e-9, str(total))


def fuzz_breakeven(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        price = rng.uniform(1e-9, 1 - 1e-9)
        fee = rng.randint(0, 9999)
        q = m.breakeven_probability(price, fee)
        check("breakeven range", 0.0 < q < 1.0, f"{q} price={price} fee={fee}")
        check("breakeven >= price", q >= price - 1e-12, f"{q} vs {price}")
        # By construction, expected value at q must be exactly zero.
        ev = q * (1 - price) / price * (1 - fee / 10_000) - (1 - q)
        check("breakeven zero EV", abs(ev) < 1e-9, f"ev={ev}")


def fuzz_reservation(rng: random.Random, trials: int) -> None:
    """A reservation price that fails its own gate is worse than no price."""
    for _ in range(trials):
        prob = rng.uniform(0.02, 0.98)
        fee = rng.choice([0, 50, 200, 500, 1000])
        c = cfg(min_edge=rng.uniform(0.005, 0.10),
                min_edge_ratio=rng.uniform(0.0, 0.5),
                min_entry_price=0.02, max_entry_price=0.98,
                max_blended_price=0.90,
                min_win_return=rng.choice([0.0, 0.10, 0.25]))
        buy = m.buy_reservation_price(prob, c, fee)
        if buy is not None:
            check("buy reservation in band",
                  c.min_entry_price <= buy <= c.max_entry_price,
                  f"{buy} outside [{c.min_entry_price}, {c.max_entry_price}]")
            check("buy reservation clears the return floor",
                  m.clears_return(buy, fee, c), f"{buy} at {fee}bps")
        sell = m.sell_reservation_price(prob, c, fee)
        if sell is not None:
            check("sell reservation beats holding",
                  sell * (1.0 - fee / 10_000.0) > prob,
                  f"{sell} nets less than holding {prob}")


def fuzz_kelly(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        c = cfg(kelly_fraction=rng.uniform(0.01, 1.0),
                max_stake_pct=rng.uniform(0.01, 0.25),
                min_stake_usdt=rng.choice([0.5, 1.0, 1.5]))
        bankroll = rng.choice([0.0, -5.0, rng.uniform(0.1, 1e6)])
        price = rng.uniform(1e-6, 1 - 1e-6)
        q = rng.uniform(0.0, 1.0)
        fee = rng.randint(0, 9999)
        try:
            stake = m.kelly_stake(bankroll, q, price, c, fee)
        except ValueError:
            continue
        check("kelly non-negative", stake >= 0.0, str(stake))
        check("kelly finite", math.isfinite(stake), str(stake))
        if bankroll > 0:
            check("kelly under hard cap",
                  stake <= bankroll * c.hard_max_stake_pct + 1e-9,
                  f"stake={stake} bankroll={bankroll}")
        if stake > 0:
            check("kelly meets minimum", stake >= c.min_stake_usdt - 1e-9,
                  str(stake))
            # Never past 2x full Kelly: beyond that, log growth is negative.
            b = ((1 - price) / price) * (1 - fee / 10_000)
            if b > 0:
                full = (q * b - (1 - q)) / b
                if full > 0:
                    check("kelly under 2x full",
                          (stake / bankroll) <= 2 * full + 1e-9,
                          f"{stake/bankroll} vs {2*full}")
        # No edge must never produce a stake.
        if q <= m.breakeven_probability(price, fee):
            check("no edge -> no stake", stake == 0.0,
                  f"q={q} be={m.breakeven_probability(price, fee)} stake={stake}")


def fuzz_walk_book(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        n = rng.randint(1, 8)
        levels = sorted((rng.uniform(0.01, 0.99), rng.uniform(0.1, 1e4))
                        for _ in range(n))
        stake = rng.uniform(0.01, 5e4)
        avg = m.walk_book(levels, stake)
        if avg is None:
            continue
        check("avg within book", levels[0][0] - 1e-9 <= avg <= levels[-1][0] + 1e-9,
              f"avg={avg} book={levels[0][0]}..{levels[-1][0]}")
        check("avg is a valid price", 0.0 < avg < 1.0, str(avg))
        shares = stake / avg
        check("shares positive", shares > 0, str(shares))

    # Hostile levels must never raise.
    for _ in range(trials // 4):
        levels = [(rng.choice(HOSTILE_FLOATS), rng.choice(HOSTILE_FLOATS))
                  for _ in range(rng.randint(1, 5))]
        try:
            m.walk_book(levels, rng.uniform(0.1, 100))
        except ValueError:
            pass
        except Exception as exc:           # noqa: BLE001 - that is the point
            check("walk_book hostile input", False,
                  f"{type(exc).__name__}: {exc} on {levels}")


def fuzz_settle(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        stake = rng.uniform(0.01, 1e5)
        price = rng.uniform(1e-6, 1 - 1e-6)
        fee = rng.randint(0, 9999)
        loss = m.settle_pnl(stake, price, False, fee)
        win = m.settle_pnl(stake, price, True, fee)
        check("loss is the stake", abs(loss + stake) < 1e-9, str(loss))
        check("win positive", win > 0, f"{win} price={price} fee={fee}")
        check("win finite", math.isfinite(win), str(win))
        # A cheaper contract must never pay less than a dearer one.
        cheaper = m.settle_pnl(stake, price / 2, True, fee)
        check("cheaper pays more", cheaper >= win - 1e-9,
              f"{cheaper} vs {win}")


def fuzz_derived_ladder(rng: random.Random, trials: int) -> None:
    """
    A derived ladder must be well-formed, and the mirror its own inverse.

    Well-formedness alone would pass a mapping that quietly dropped a level
    or clamped a price into range. The round trip is what actually pins the
    transformation: mirroring twice has to land back where it started.
    """
    for _ in range(trials):
        prices = sorted({round(rng.uniform(0.01, 0.98), 4)
                         for _ in range(rng.randint(1, 8))})
        book_bids = [(p, round(rng.uniform(0.1, 5000.0), 4)) for p in prices]
        book_asks = [(min(0.99, p + 0.01), s) for p, s in book_bids]

        for side in m.Side:
            got = ws_feeds.derive_asks(book_asks, book_bids, side)
            check("derived ladder ascends", got == sorted(got), str(got))
            check("derived prices are in (0,1)",
                  all(0.0 < p < 1.0 for p, _ in got), str(got))
            check("derived sizes are positive",
                  all(s > 0 for _, s in got), str(got))

        once = ws_feeds.derive_asks(book_asks, book_bids, m.Side.DOWN)
        twice = ws_feeds.derive_asks(once, once, m.Side.DOWN)
        check("mirror is an involution",
              len(twice) == len(book_bids)
              and all(abs(p1 - p2) < 1e-9 and abs(s1 - s2) < 1e-9
                      for (p1, s1), (p2, s2) in zip(sorted(book_bids), twice)),
              f"{sorted(book_bids)} -> {twice}")


def fuzz_wei(rng: random.Random, trials: int) -> None:
    for _ in range(trials):
        amount = rng.choice([
            Decimal(str(round(rng.uniform(1e-9, 1e6), 9))),
            Decimal("1"), Decimal("0.000000000000000001"),
        ])
        wei = m.to_wei(amount)
        check("wei is an integer string", wei.isdigit(), wei)
        back = m.from_wei(wei)
        check("wei round trip", abs(back - amount) <= Decimal("1e-18"),
              f"{amount} -> {wei} -> {back}")
        check("wei never rounds up", back <= amount, f"{back} > {amount}")


# --------------------------------------------------------------------------
# Payload robustness
# --------------------------------------------------------------------------


def random_json(rng: random.Random, depth: int = 0):
    """An arbitrary JSON-ish value, including hostile scalars."""
    if depth > 3:
        return rng.choice([None, 0, "", [], {}])
    kind = rng.randint(0, 7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice(HOSTILE_FLOATS)
    if kind == 2:
        return rng.choice(["", "0", "abc", "-1", "1e999", "NaN", "null",
                           "0x00", " ", "\x00"])
    if kind == 3:
        return rng.randint(-10**18, 10**18)
    if kind == 4:
        return rng.choice([True, False])
    if kind == 5:
        return [random_json(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    if kind == 6:
        return {rng.choice(["price", "size", "tokenId", "name", "marketId",
                            "status", "feeRateBps", "startPrice", "asks"]):
                random_json(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    return rng.choice(["YES", "NO", "UP", "DOWN", "OPEN", "REGISTERED"])


def fuzz_parsers(rng: random.Random, trials: int) -> None:
    """Every parser must return a value or None -- never raise."""
    for _ in range(trials):
        payload = random_json(rng)
        for fn, label in (
            (m.PredictionClient._parse_round, "_parse_round"),
            (m.PredictionClient._parse_asks, "_parse_asks"),
            (m.PredictionClient._parse_variant, "_parse_variant"),
        ):
            if not isinstance(payload, dict):
                continue
            try:
                fn(payload)
            except Exception as exc:       # noqa: BLE001 - that is the point
                check(f"{label} hostile payload", False,
                      f"{type(exc).__name__}: {exc} on {str(payload)[:120]}")

    # Well-shaped but hostile-valued market topics.
    for _ in range(trials // 2):
        topic = {
            "marketTopicId": random_json(rng), "chartType": "CRYPTO_UP_DOWN",
            "symbol": "BTCUSDT", "status": "REGISTERED",
            "startDate": rng.choice([0, 1748131200000, random_json(rng)]),
            "endDate": rng.choice([300000, 1748131500000, random_json(rng)]),
            "vendor": "V", "chainId": "56", "collateral": "USDT",
            "feeRateBps": random_json(rng), "slippageBps": random_json(rng),
            "liquidity": random_json(rng),
            "variantData": random_json(rng),
            "markets": [{"marketId": random_json(rng), "tradingStatus": "OPEN",
                         "decimalPrecision": random_json(rng),
                         "liquidity": random_json(rng),
                         "outcomes": [
                             {"name": "YES", "price": random_json(rng),
                              "tokenId": "1"},
                             {"name": "NO", "price": random_json(rng),
                              "tokenId": "2"}]}],
        }
        try:
            rnd = m.PredictionClient._parse_round(topic)
        except Exception as exc:           # noqa: BLE001
            check("_parse_round hostile topic", False,
                  f"{type(exc).__name__}: {exc}")
            continue
        if rnd is not None:
            check("parsed fee in range", 0 <= rnd.fee_bps < 10_000,
                  str(rnd.fee_bps))
            check("parsed prices valid",
                  0 < rnd.up_quote < 1 and 0 < rnd.down_quote < 1,
                  f"{rnd.up_quote}/{rnd.down_quote}")
            check("parsed precision sane", rnd.decimal_precision >= 0,
                  str(rnd.decimal_precision))


def fuzz_assess(rng: random.Random, trials: int) -> None:
    """Any Signal returned must satisfy every configured constraint."""
    for _ in range(trials):
        c = cfg(**m.PROFILES[rng.choice(list(m.PROFILES))])
        strike = rng.uniform(5e4, 1.5e5)
        # Exercise more than one market: `symbol` (the venue's market ticker)
        # and `feed_symbol` (the oracle it settles against) are independent
        # fields and can diverge, so fuzz them independently rather than
        # assuming every round is BTCUSDT-on-BTCUSDT.
        symbol = rng.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        rnd = m.Round(
            topic_id=1, market_id=1, vendor="V", slug="s", symbol=symbol,
            start_ms=0, end_ms=c.round_seconds * 1000,
            up_token_id="1", down_token_id="2",
            up_quote=rng.uniform(0.01, 0.99), down_quote=rng.uniform(0.01, 0.99),
            fee_bps=rng.randint(0, 900), chain_id="56", collateral="USDT",
            venue_slippage_bps=1200, decimal_precision=2,
            liquidity=rng.uniform(0, 1e6), strike=strike, feed_symbol="BTCUSDT")
        bankroll = rng.uniform(1, 1e5)
        now = rnd.end_ms - int(rng.uniform(0, 320) * 1000)
        book = None
        if rng.random() < 0.7:
            book = {}
            for side in m.Side:
                base = rng.uniform(0.02, 0.97)
                book[side] = sorted(
                    (min(base + i * rng.uniform(0, .05), 0.99),
                     rng.uniform(1, 1e5)) for i in range(rng.randint(1, 4)))
        # A trend that may be confirmed, unconfirmed, dying or reversing.
        # The boost widens the window and multiplies the stake, so every
        # invariant below has to hold with it in play, not only without it.
        trend = m.Trend(
            direction=rng.choice([-1, 0, 1]),
            impulse=rng.choice([0.0, 0.5, 1.3, 4.0, 1e6]),
            z=rng.uniform(0.0, 6.0), efficiency=rng.uniform(0.0, 1.0),
            run=rng.randint(0, 9), decay=rng.uniform(0.0, 1.5),
            rounds_left=rng.choice([0.0, 0.5, 1.0, 3.0, 99.0]),
            phase=rng.choice(["none", "building", "running", "fading"]))
        try:
            verdict = m.assess(rnd, strike * rng.uniform(0.97, 1.03),
                               rng.uniform(0.05, 3.0), bankroll,
                               now, c, book, rng.choice([None, 3.0, 6.0]),
                               trend)
        except Exception as exc:           # noqa: BLE001
            check("assess hostile input", False,
                  f"{type(exc).__name__}: {exc}")
            continue
        sig = verdict.signal
        if sig is None:
            # A declined round must always say why, or the operator cannot
            # tell a quiet market from a broken one.
            check("decline is explained", bool(verdict.blocked_by),
                  "empty reason")
            check("decline reason is known",
                  verdict.blocked_by in m._DECLINE_ORDER, verdict.blocked_by)
            continue
        check("signal price band",
              c.min_entry_price <= sig.fill_price <= c.max_entry_price,
              f"{sig.fill_price} not in [{c.min_entry_price},{c.max_entry_price}]")
        check("signal edge clears floor", sig.edge >= c.min_edge - 1e-12,
              f"{sig.edge} < {c.min_edge}")
        check("signal stake positive", sig.stake_usdt > 0, str(sig.stake_usdt))
        check("signal within entry window",
              c.entry_window_end_s <= sig.seconds_left
              <= m.entry_window_start_s(c, sig.trend_boosted),
              str(sig.seconds_left))
        # An early entry is only ever granted to the side the trend points
        # at, and only while that trend is confirmed.
        if sig.seconds_left > c.entry_window_start_s:
            check("early entry implies a confirmed aligned trend",
                  sig.trend_boosted and trend.confirmed(c)
                  and trend.favours(sig.side),
                  f"{sig.seconds_left}s left without a trend")
        # The return floor binds on the price actually paid.
        check("signal clears the return floor",
              m.clears_return(sig.fill_price, rnd.fee_bps, c),
              f"{sig.fill_price} pays "
              f"{m.win_return(sig.fill_price, rnd.fee_bps):.4f} "
              f"< {c.min_win_return}")
        # Boosted or not, no stake may pass twice full Kelly. Measured
        # against the same bankroll the sizing used, or the ratio is
        # meaningless.
        mult = m.kelly_multiple(sig.stake_usdt, bankroll, sig.model_prob,
                                sig.fill_price, rnd.fee_bps)
        if mult is not None:
            check("stake never passes 2x full Kelly", mult <= 2.0 + 1e-6,
                  f"{mult:.3f}x")
        check("signal prob valid", 0.0 <= sig.model_prob <= 1.0,
              str(sig.model_prob))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    suites = [
        ("digital pricing", fuzz_digital),
        ("breakeven", fuzz_breakeven),
        ("reservation price", fuzz_reservation),
        ("kelly sizing", fuzz_kelly),
        ("order book walking", fuzz_walk_book),
        ("derived ladder", fuzz_derived_ladder),
        ("settlement P&L", fuzz_settle),
        ("wei conversion", fuzz_wei),
        ("payload parsers", fuzz_parsers),
        ("assess", fuzz_assess),
    ]
    for name, fn in suites:
        before = len(failures)
        fn(rng, args.trials)
        found = len(failures) - before
        print(f"  {name:<22} {'FAIL ' + str(found) if found else 'ok'}")

    print()
    if failures:
        print(f"{len(failures)} invariant violation(s); first 15:\n")
        seen = set()
        shown = 0
        for f in failures:
            key = f.split(":")[0]
            if key in seen:
                continue
            seen.add(key)
            print(f"  - {f[:180]}")
            shown += 1
            if shown >= 15:
                break
        return 1
    print("All invariants held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
