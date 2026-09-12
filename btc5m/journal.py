"""
The record of what was traded and what it paid -- the only thing that
can say whether the strategy has an edge.
"""

from __future__ import annotations

import math
import sqlite3
import time

from btc5m.constants import DEFAULT_FEE_BPS, EPS
from btc5m.pricing import breakeven_probability

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Round, Signal

class Journal:
    """Append-only record of every decision, for calibration analysis."""

    def __init__(self, path: str, profile: str = "unknown") -> None:
        self._profile = profile
        self._conn = sqlite3.connect(path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER, mode TEXT, slug TEXT, topic_id INTEGER, side TEXT,
                strike REAL, spot REAL, sigma REAL, seconds_left REAL,
                end_ms INTEGER, model_prob REAL, fill_price REAL, edge REAL,
                stake REAL, bankroll_before REAL, order_id TEXT,
                profile TEXT, buffer_z REAL, fee_bps INTEGER,
                symbol TEXT, trend_z REAL,
                resolved INTEGER DEFAULT 0, won INTEGER, pnl REAL,
                settle_source TEXT, order_type TEXT, price_limit REAL,
                exit_price REAL, exit_order_id TEXT)""")
        # Journals predating the profile column stay readable.
        existing = {r[1] for r in
                    self._conn.execute("PRAGMA table_info(trades)")}
        if "profile" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN profile TEXT")
        if "buffer_z" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN buffer_z REAL")
        if "fee_bps" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN fee_bps INTEGER")
        if "symbol" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN symbol TEXT")
        if "trend_z" not in existing:
            self._conn.execute("ALTER TABLE trades ADD COLUMN trend_z REAL")
        for col, decl in (("order_type", "TEXT"), ("price_limit", "REAL"),
                          ("exit_price", "REAL"), ("exit_order_id", "TEXT")):
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {col} {decl}")
        self._conn.commit()

    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None,
               order_type: str = "MARKET",
               price_limit: float | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (ts, mode, slug, topic_id, side, strike, spot,"
            " sigma, seconds_left, end_ms, model_prob, fill_price, edge, stake,"
            " bankroll_before, order_id, profile, buffer_z, fee_bps, symbol,"
            " trend_z, order_type, price_limit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), mode, rnd.slug, rnd.topic_id, sig.side.value,
             rnd.strike, spot, sigma, sig.seconds_left, rnd.end_ms,
             sig.model_prob, sig.fill_price, sig.edge, sig.stake_usdt,
             bankroll, order_id, self._profile, sig.buffer_z, rnd.fee_bps,
             rnd.symbol, sig.trend_z, order_type, price_limit))
        self._conn.commit()
        return int(cur.lastrowid)

    def resolve(self, trade_id: int, won: bool, pnl: float,
                source: str) -> None:
        self._conn.execute(
            "UPDATE trades SET resolved=1, won=?, pnl=?, settle_source=?"
            " WHERE id=?", (1 if won else 0, pnl, source, trade_id))
        self._conn.commit()

    def close(self) -> None:
        """
        Release the SQLite handle.

        The connection was never closed, which on Windows means the file
        cannot be unlinked while the process lives -- every test that removes
        its temp journal fails on the unlink, and the real failure is hidden
        behind a cleanup error. A long-running process leaks one handle per
        Journal, which is small but no more correct.
        """
        self._conn.close()

    def resolve_sold(self, trade_id: int, proceeds_usdt: float,
                     exit_price: float, exit_order_id: str,
                     stake: float) -> None:
        """
        Close a row that was sold rather than settled.

        P&L is proceeds minus stake, and `won` is deliberately left NULL: the
        round's outcome never applied to this position, and writing 0 or 1
        would answer a question the trade did not ask. settle_source records
        which kind of ending this was, so diagnose() can keep the two apart.
        """
        self._conn.execute(
            "UPDATE trades SET resolved=1, pnl=?, settle_source='sold',"
            " exit_price=?, exit_order_id=? WHERE id=?",
            (proceeds_usdt - stake, exit_price, exit_order_id, trade_id))
        self._conn.commit()

    def diagnose(self, profile: str | None = None,
                 symbol: str | None = None) -> str:
        """
        Is the edge real, and if not, what would fix it?

        Answers one question per price bucket: was the realised win rate above
        the breakeven implied by the price paid? That single comparison
        decides everything. A losing run at a good win rate and a winning run
        at a poor one look identical over a few dozen trades, so the shortfall
        is reported with a standard error rather than as a bare number.
        """
        # Sold rows are resolved but have no outcome to be calibrated
        # against: the position was closed before the round decided
        # anything. Pooling them with settled rows would let an exit taken at
        # a good price look like a correct prediction, which is exactly the
        # inference this report exists to make impossible.
        where = "WHERE resolved=1 AND COALESCE(settle_source, '') != 'sold'"
        args: tuple = ()
        if profile:
            where += " AND COALESCE(profile, 'unknown') = ?"
            args = (profile,)
        if symbol:
            where += " AND COALESCE(symbol, 'unknown') = ?"
            args = args + (symbol,)
        rows = self._conn.execute(
            f"SELECT fill_price, won, pnl, stake, fee_bps FROM trades {where}",
            args).fetchall()
        sold = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades"
            " WHERE resolved=1 AND settle_source='sold'").fetchone()
        if not rows:
            return "No resolved trades yet."

        buckets: dict[int, list] = {}
        for price, won, pnl, stake, fee in rows:
            buckets.setdefault(int(price * 20), []).append(
                (price, won, pnl or 0.0, stake or 0.0, fee))

        scope = symbol or "all markets"
        out = [f"Trades analysed : {len(rows)}  ({scope})"]
        if sold and sold[0]:
            out.append(f"Excluded        : {sold[0]} sold before settlement "
                       f"({sold[1]:+.2f} USDT)")
        out += ["",
               "Realised win rate vs the breakeven for the price paid",
               "(breakeven uses each market's own published fee):",
               "  price band      n   needed   actual     gap      P&L  verdict"]
        total_gap_n = 0
        verdicts: list[tuple[str, float, int]] = []

        for b in sorted(buckets):
            vals = buckets[b]
            n = len(vals)
            avg_price = sum(v[0] for v in vals) / n
            # Each market publishes its own fee; assuming 2% shifts the exact
            # bar this whole verdict is measured against. Rows predating the
            # column fall back to the configured default.
            fees = [v[4] for v in vals if v[4] is not None]
            fee = int(sum(fees) / len(fees)) if fees else DEFAULT_FEE_BPS
            needed = breakeven_probability(avg_price, fee)
            actual = sum(v[1] for v in vals) / n
            pnl = sum(v[2] for v in vals)
            gap = actual - needed
            se = math.sqrt(max(actual * (1 - actual), EPS) / n)

            if n < 20:
                verdict = "too few"
            elif gap > 2 * se:
                verdict = "EDGE"
            elif gap < -2 * se:
                verdict = "NO EDGE"
            else:
                verdict = "unclear"
            verdicts.append((verdict, gap, n))
            total_gap_n += n
            out.append(f"  {b/20:.2f}-{b/20+0.05:.2f} {n:>6} {needed:>8.1%} "
                       f"{actual:>8.1%} {gap:>+7.1%} {pnl:>+8.2f}  {verdict}")

        out += ["", "=" * 66, "WHAT THIS MEANS", "=" * 66]
        losing = [v for v in verdicts if v[0] == "NO EDGE"]
        winning = [v for v in verdicts if v[0] == "EDGE"]
        unclear = [v for v in verdicts if v[0] in ("unclear", "too few")]

        if losing and not winning:
            out += [
                "",
                "Your win rate is significantly BELOW breakeven. Raising the",
                "stake cannot fix this: stake multiplies expected value, it",
                "cannot change its sign. A bigger bet on a negative edge just",
                "loses faster.",
                "",
                "The levers that do work, in order of directness:",
                "  1. Demand a bigger buffer (--min-buffer 2.5 or 3.0). More",
                "     standard deviations from the strike means a genuinely",
                "     higher win probability, not just a higher price.",
                "  2. Enter later (lower entry_window_start_s). The same",
                "     buffer is worth more with less time left to reverse.",
                "  3. Pay less (lower max_entry_price). A worse win rate but a",
                "     better win:loss ratio, and a lower bar to clear.",
                "  4. Stop. If none of the above lifts the actual column above",
                "     the needed column, the edge is not there to be found.",
            ]
        elif winning and not losing:
            out += [
                "",
                "Your win rate is significantly ABOVE breakeven in the bands",
                "marked EDGE. Sizing up there is correct, and is what Kelly",
                "already does automatically -- the bot raises stake as the",
                "edge grows, without any change from you.",
                "",
                "Do NOT raise the stake cap by hand to 'cover' losses. The",
                "losses are already priced in; the cap is what keeps a run of",
                "them survivable.",
            ]
        else:
            out += [
                "",
                (f"Inconclusive: {len(unclear)} band(s) lack the sample to call,"
                f" {len(winning)} show edge, {len(losing)} show none."),
                "",
                "Over a few dozen trades a 68% win rate and a 76% win rate are",
                "indistinguishable, yet one loses money and the other compounds.",
                "Keep the settings fixed and let the sample grow. Changing size",
                "in response to a losing streak is the one move that converts",
                "an unclear result into a certain loss.",
            ]
        return "\n".join(out)

    def calibration_report(self, profile: str | None = None) -> str:
        """
        Report per profile, or every profile in turn.

        Profiles trade disjoint price bands, so pooling them averages a
        longshot strategy together with a favourite strategy and reports a
        bias that belongs to neither.
        """
        profiles = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT COALESCE(profile, 'unknown') FROM trades "
            "WHERE resolved=1 ORDER BY 1")]
        if not profiles:
            return "No resolved trades yet. Let paper mode run first."

        if profile is None and len(profiles) > 1:
            parts = [(f"Journal contains {len(profiles)} profiles: "
                     f"{', '.join(profiles)}"), ""]
            for name in profiles:
                parts.append(f"{'=' * 62}\nPROFILE: {name}\n{'=' * 62}")
                parts.append(self.calibration_report(name))
                parts.append("")
            return "\n".join(parts)

        target = profile or profiles[0]
        rows = self._conn.execute(
            "SELECT model_prob, won, pnl, stake FROM trades "
            "WHERE resolved=1 AND COALESCE(profile, 'unknown') = ?",
            (target,)).fetchall()
        if not rows:
            return f"No resolved trades for profile {target!r}."

        buckets: dict[int, list[tuple[float, int]]] = {}
        for prob, won, _, _ in rows:
            buckets.setdefault(min(int(prob * 10), 9), []).append((prob, won))

        n = len(rows)
        markets = [r[0] for r in self._conn.execute(
            "SELECT DISTINCT COALESCE(symbol, 'unknown') FROM trades "
            "WHERE resolved=1 AND COALESCE(profile, 'unknown') = ? ORDER BY 1",
            (target,))]
        header = [f"Profile         : {target}",
                  f"Markets         : {', '.join(markets)}"]
        pnl = sum(r[2] or 0.0 for r in rows)
        staked = sum(r[3] or 0.0 for r in rows)
        lines = header + [
            f"Resolved trades : {n}",
            f"Total P&L       : {pnl:+.2f} USDT",
            f"Return on stake : {(pnl / staked if staked else 0):+.2%}",
            f"Hit rate        : {sum(r[1] for r in rows) / n:.1%}",
            "",
            "Calibration (model says X% -> actually won Y%):",
            "  bucket        n   predicted    actual      gap",
        ]
        for b in sorted(buckets):
            vals = buckets[b]
            pred = sum(p for p, _ in vals) / len(vals)
            act = sum(w for _, w in vals) / len(vals)
            se = math.sqrt(max(act * (1 - act), EPS) / len(vals))
            flag = "" if abs(act - pred) <= 2 * se else "  <-- off"
            lines.append(f"  {b*10:>3}-{b*10+9:<3} {len(vals):>6}"
                         f"   {pred:>8.1%} {act:>9.1%} {act-pred:>+8.1%}{flag}")
        # Market-price buckets: does the venue's own price predict outcomes?
        price_rows = self._conn.execute(
            "SELECT fill_price, won FROM trades WHERE resolved=1 "
            "AND COALESCE(profile, 'unknown') = ?", (target,)).fetchall()
        pbuckets: dict[int, list[tuple[float, int]]] = {}
        for price, won in price_rows:
            pbuckets.setdefault(min(int(price * 10), 9), []).append((price, won))

        if len(markets) > 1:
            lines += ["", "Per market (each trades independently):",
                      "  market            n   hit rate       P&L"]
            for mk in markets:
                mrows = self._conn.execute(
                    "SELECT won, pnl FROM trades WHERE resolved=1 "
                    "AND COALESCE(profile, 'unknown') = ? "
                    "AND COALESCE(symbol, 'unknown') = ?",
                    (target, mk)).fetchall()
                if not mrows:
                    continue
                wins = sum(w for w, _ in mrows)
                pnl_m = sum(p or 0.0 for _, p in mrows)
                lines.append(f"  {mk:<12} {len(mrows):>6} "
                             f"{wins/len(mrows):>9.1%} {pnl_m:>+9.2f}")

        lines += [
            "",
            "Favourite-longshot bias (market price vs realised frequency):",
            "  price       n   implied     actual      gap",
        ]
        if n < 30:
            lines.append(f"  (only {n} trade(s): far too few to read a bias "
                         f"from -- shown for completeness, not as a verdict)")
        for b in sorted(pbuckets):
            vals = pbuckets[b]
            imp = sum(p for p, _ in vals) / len(vals)
            act = sum(w for _, w in vals) / len(vals)
            se = math.sqrt(max(act * (1 - act), EPS) / len(vals))
            flag = ""
            if abs(act - imp) > 2 * se:
                flag = "  <-- underpriced" if act > imp else "  <-- overpriced"
            lines.append(f"  {b/10:.1f}-{b/10+0.1:.1f} {len(vals):>6}"
                         f"   {imp:>8.1%} {act:>9.1%} {act-imp:>+8.1%}{flag}")
        lines += [
            "",
            "A positive gap means contracts at that price win MORE often than",
            "their price implies -- that band is underpriced and worth buying.",
            "If high prices show positive gaps and low prices negative ones,",
            "the market has a favourite-longshot bias and buying favourites is",
            "the right side. The reverse favours the convex profile. This is",
            "the measurement that decides between them; nothing else does.",
        ]

        # Buffer buckets: is the model reliable where it claims near-certainty?
        z_rows = self._conn.execute(
            "SELECT buffer_z, won, pnl FROM trades WHERE resolved=1 "
            "AND buffer_z IS NOT NULL AND buffer_z != 0 "
            "AND COALESCE(profile, 'unknown') = ?", (target,)).fetchall()
        if z_rows:
            edges = [(0, 1), (1, 2), (2, 3), (3, 5), (5, 1e9)]
            lines += [
                "",
                "By buffer (|z| = distance from strike in sigmas of time left):",
                "  buffer        n   win rate      P&L",
            ]
            for lo, hi in edges:
                vals = [(w, p or 0.0) for zz, w, p in z_rows
                        if lo <= abs(zz) < hi]
                if not vals:
                    continue
                wins = sum(w for w, _ in vals)
                pnl = sum(p for _, p in vals)
                label = f"{lo}-{hi}" if hi < 1e9 else f"{lo}+"
                lines.append(f"  {label:<8} {len(vals):>6} "
                             f"{wins/len(vals):>9.1%} {pnl:>+9.2f}")
            lines += [
                "",
                "A big buffer should show a high win rate AND positive P&L. A",
                "high win rate with negative P&L means the wins are too small",
                "to pay for the rare losses -- the exact failure mode of",
                "trading near-certainties, and only this table reveals it.",
            ]

        lines += [
            "",
            "If 'actual' sits consistently below 'predicted', the model is",
            "overconfident and every edge estimate is inflated. Do not trade",
            "real money until the gap column is small and unbiased across",
            "buckets over several hundred trades. Rows flagged '<-- off' differ",
            "from prediction by more than two standard errors.",
        ]
        return "\n".join(lines)
