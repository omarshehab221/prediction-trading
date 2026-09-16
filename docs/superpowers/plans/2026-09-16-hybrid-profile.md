# Hybrid Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `hybrid` profile. Each round it tries a locked straddle first,
falls back to a buffer entry, puts a 25% stop-loss under every position that can
still lose, and trades a balance whose 20% / 10% stake falls under the $1
minimum.

**Architecture:** A new `hybrid` config flag dispatches to
`HybridMixin._maybe_enter_hybrid`, which runs the existing straddle path and
then the existing model (buffer) path. A few `cfg.hybrid` branches inside those
paths add the $1 floors and stop the two from blocking each other.
`_sync_hybrid_stops` is declarative and runs once per loop pass. It arms a
stop-only `Bracket` on every unpaired position and drops brackets on completed
pairs. The existing `_check_stops` fires those brackets, and skips any round
inside its final `hybrid_stop_disarm_s`. The `straddle`, `buffer` and `scalp`
profiles behave exactly as before, because every new branch is gated on
`cfg.hybrid`.

**Tech Stack:** Python 3.11/3.12, stdlib `unittest` (no pytest installed),
sqlite journal, the project's fake venue clients in `tests/support.py`.

**Spec:** `docs/superpowers/specs/2026-09-16-hybrid-profile-design.md`

## Global Constraints

- Work on `master`. Run `git branch --show-current` before every commit. If it
  is not `master`, stop: another session may have the tree on `rust-port`.
- Every new behaviour is gated on `cfg.hybrid`. Existing profiles must not
  change. Their existing tests are the proof.
- `hybrid_stop_loss_pct: 0.25`, validated in (0, 1).
- `hybrid_stop_disarm_s: 45.0`, validated >= 0.
- `min_stake_usdt: 1.0`. Straddle legs stake `max(bankroll x 0.20, 1.00)`.
  Buffer stakes `max(Kelly stake, 1.00)`, capped at free funds.
- A straddle first leg opens only when free funds are at least `2 x min_stake_usdt`.
- A completed straddle pair (both sides held on one symbol) is never stopped.
- No take-profit and no flatten on hybrid.
- Hybrid requires `exit_order_type: "NONE"` and `entry_order_type: "MARKET"`.
  It excludes `scalp`, `straddle` and `last_minute`.
- Match the surrounding style: long explanatory comments on *why*, not what.
- Run tests with `python -m unittest <module>.<Class>[.<test>] -v` from the
  repo root. Do **not** run the full suite on Windows (10+ minutes). The gate
  is `verify.sh` in WSL (Task 8).
- Commit messages end with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `btc5m/config.py` | `hybrid`, `hybrid_stop_loss_pct`, `hybrid_stop_disarm_s`; validation | 1 |
| `btc5m/profiles.py` | `hybrid` profile entry | 1 |
| `tests/support.py` | `hybrid_cfg()` | 1 |
| `btc5m/trader/hybrid.py` (new) | `HybridMixin`: dispatch, stop sync | 2, 6 |
| `btc5m/trader/core.py` | mixin, dispatch branch, `_hybrid_stopped` state, loop call | 2, 6 |
| `btc5m/trader/straddle.py` | no `_seen` on window expiry; $1 floors | 2, 4 |
| `btc5m/trader/model_entry.py` | skip straddle's opening minute; delivered shares | 2, 5 |
| `btc5m/sizing.py` | Kelly floor, boost never shrinks, completion band | 3, 4 |
| `btc5m/trader/scale_in.py` | delivered shares on top-ups | 5 |
| `btc5m/trader/scalp.py` | `_check_stops` runs for hybrid, disarm window | 6 |
| `btc5m/probes.py` | `hybrid_balance_notes`; preflight branch | 7 |
| `README.md`, `deploy/aws/deploy.sh` | docs, profile whitelist | 8 |
| `tests/test_config.py`, `tests/test_strategies.py`, `tests/test_pricing.py`, `tests/test_trader.py`, `tests/test_cli.py` | tests | all |

---

### Task 1: Config fields, validation, and the `hybrid` profile

**Files:**
- Modify: `btc5m/config.py`: fields after the scalp block (~line 506); validation in the strategy-exclusivity block (~lines 846-870)
- Modify: `btc5m/profiles.py`: new entry after `"lastminute"` (before the closing `}` at ~line 509)
- Modify: `tests/support.py`: add `hybrid_cfg` beside `scalp_cfg`
- Test: `tests/test_config.py`: new `TestHybridConfig`

**Interfaces:**
- Produces: `Config.hybrid: bool`, `Config.hybrid_stop_loss_pct: float`, `Config.hybrid_stop_disarm_s: float`; `m.PROFILES["hybrid"]`; `tests.support.hybrid_cfg(**kw) -> Config`

- [ ] **Step 1: Add `hybrid_cfg` to `tests/support.py`** (after `scalp_cfg`)

```python
def hybrid_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False,
                **m.PROFILES["hybrid"])
    base.update(kw)
    return Config(**base)
```

- [ ] **Step 2: Write the failing tests**. Append to `tests/test_config.py` and add `hybrid_cfg` to its `tests.support` import.

```python
class TestHybridConfig(unittest.TestCase):

    def test_the_profile_builds(self):
        c = hybrid_cfg()
        self.assertTrue(c.hybrid)
        self.assertFalse(c.straddle or c.scalp or c.last_minute)

    def test_the_profile_carries_the_agreed_numbers(self):
        c = hybrid_cfg()
        self.assertEqual(c.hybrid_stop_loss_pct, 0.25)
        self.assertEqual(c.hybrid_stop_disarm_s, 45.0)
        self.assertEqual(c.straddle_stake_pct, 0.20)
        self.assertEqual(c.max_stake_pct, 0.10)
        self.assertEqual(c.min_stake_usdt, 1.0)
        self.assertEqual(c.entry_window_start_s, 240)
        self.assertEqual(c.straddle_entry_window_s, 60.0)
        self.assertEqual(c.daily_loss_limit_pct, 0.50)
        self.assertEqual(c.reserve_pct, 0.10)
        self.assertEqual(c.max_concurrent_positions, 4)
        self.assertTrue(c.straddle_require_positive_worst_case)
        self.assertFalse(c.straddle_force_hedge)
        self.assertTrue(c.scale_in)
        self.assertTrue(c.trend_follow)

    def test_hybrid_excludes_every_other_entry_strategy(self):
        for flag in ("straddle", "scalp", "last_minute"):
            with self.assertRaises(ValueError, msg=flag):
                hybrid_cfg(**{flag: True})

    def test_hybrid_refuses_any_exit_but_none(self):
        for exit_type in ("MARKET", "LIMIT", "BRACKET"):
            with self.assertRaises(ValueError, msg=exit_type):
                hybrid_cfg(exit_order_type=exit_type)

    def test_hybrid_refuses_limit_entries(self):
        with self.assertRaises(ValueError):
            hybrid_cfg(entry_order_type="LIMIT")

    def test_stop_loss_pct_must_be_in_zero_one(self):
        for bad in (0.0, 1.0, -0.1):
            with self.assertRaises(ValueError, msg=bad):
                hybrid_cfg(hybrid_stop_loss_pct=bad)

    def test_disarm_window_must_be_non_negative(self):
        with self.assertRaises(ValueError):
            hybrid_cfg(hybrid_stop_disarm_s=-1.0)

    def test_hybrid_is_off_everywhere_else(self):
        for name, values in m.PROFILES.items():
            if name != "hybrid":
                self.assertNotIn("hybrid", values, name)
```

- [ ] **Step 3: Run to verify failure**

Run: `python -m unittest tests.test_config.TestHybridConfig -v`
Expected: ERROR, `KeyError: 'hybrid'` from `hybrid_cfg`.

- [ ] **Step 4: Add the fields to `Config`** (after `scalp_max_edge_required`, ~line 506)

```python
    # Straddle, buffer and a stop, layered per round. See
    # docs/superpowers/specs/2026-09-16-hybrid-profile-design.md.
    #
    # Not a fifth entry path: it runs the straddle path, then the model path,
    # and puts a watched stop under whatever those opened that can still lose.
    hybrid: bool = False
    # A P&L fraction below the executed entry price, not a price move. Wide on
    # purpose: buffer enters at 0.55-0.80, where a 5% stop fires on noise, and
    # a stop sells into a falling bid and has overshot about 2x live.
    hybrid_stop_loss_pct: float = 0.25
    # Inside this many seconds before settlement no stop fires. The book is
    # thinnest there, and a sale turns a coin flip into a certain loss.
    hybrid_stop_disarm_s: float = 45.0
```

- [ ] **Step 5: Add validation.** Put it next to the scalp range checks (after `scalp_max_edge_required`'s check):

```python
        if not 0 < self.hybrid_stop_loss_pct < 1.0:
            raise ValueError("hybrid_stop_loss_pct must be in (0, 1)")
        if self.hybrid_stop_disarm_s < 0:
            raise ValueError("hybrid_stop_disarm_s must be non-negative")
```

Replace the `chosen = [...]` block with one that includes hybrid, and add the two hybrid-only order-type rules after it:

```python
        chosen = [n for n, on in (("straddle", self.straddle),
                                  ("last_minute", self.last_minute),
                                  ("scalp", self.scalp),
                                  ("hybrid", self.hybrid)) if on]
        if len(chosen) > 1:
            raise ValueError(
                f"only one entry strategy may be enabled, got: "
                f"{', '.join(chosen)}")
        if self.hybrid and self.exit_order_type != "NONE":
            # The stop is hybrid's own mechanism. A model-priced exit would
            # offer a straddle leg back to the book, and unlock the pair.
            raise ValueError(
                f"hybrid requires exit_order_type NONE, got "
                f"{self.exit_order_type!r}")
        if self.hybrid and self.entry_order_type != "MARKET":
            # A resting buffer entry that fills after the straddle window
            # reopens the market would put two strategies on one symbol.
            raise ValueError(
                f"hybrid requires entry_order_type MARKET, got "
                f"{self.entry_order_type!r}")
```

- [ ] **Step 6: Add the profile.** In `btc5m/profiles.py`, insert after the `"lastminute"` entry:

```python
    # STRADDLE WHEN IT LOCKS, BUFFER WHEN IT DOESN'T, A STOP UNDER EITHER.
    #
    # The first minute belongs to the straddle path, unchanged: a pair is
    # taken only when both payouts beat what it cost. A market holding no leg
    # after that minute is offered to the buffer path, unchanged too. Whatever
    # is open and can still lose -- a buffer position, or a first leg whose
    # partner never came -- carries a stop 25% under its executed price. A
    # completed pair carries none: its outcome is already locked, and selling
    # one leg would unlock it. No take-profit: buffer's winners pay 25%+ at
    # settlement, and scalp's 12% target would sell them for half that.
    #
    # SMALL BALANCES. Both stakes are floored at the 1.00 venue minimum rather
    # than refused -- straddle legs under 5.00, buffer under 10.00. At 3.00 a
    # 1.00 stake is 33% of bankroll, past where Kelly says growth turns
    # negative. That is asked for, and logged on every floored entry.
    "hybrid": {"hybrid": True,
               "hybrid_stop_loss_pct": 0.25, "hybrid_stop_disarm_s": 45.0,
               # -- the straddle layer: the straddle profile's own numbers --
               "straddle_stake_pct": 0.20, "straddle_entry_window_s": 60.0,
               "straddle_require_positive_worst_case": True,
               "straddle_min_worst_case_return": 0.0,
               "straddle_force_hedge": False,
               # -- the buffer layer: the buffer profile's own gates --
               "max_entry_price": 0.80, "min_entry_price": 0.55,
               "min_edge": 0.012, "min_edge_ratio": 0.010,
               "min_win_return": 0.25, "min_buffer_sigmas": 0.75,
               # The buffer cap. Straddle legs size off straddle_stake_pct
               # and never read this; the two are different layers' stakes,
               # not one number disagreeing with itself.
               "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
               "max_price_impact": 0.05, "assumed_spread_pct": 0.03,
               "kelly_fraction": 0.25, "min_liquidity": 150.0,
               "scale_in": True, "scale_in_initial_pct": 0.25,
               "scale_in_min_topup": 1.0, "max_blended_price": 0.78,
               "trend_follow": True,
               "trend_min_impulse": 1.2, "trend_min_run": 1,
               "trend_max_run": 5, "trend_decay_floor": 0.55,
               "trend_min_rounds_left": 1.0, "trend_min_z": 0.8,
               "trend_min_efficiency": 0.40,
               "trend_stake_multiple": 1.5, "trend_early_entry_s": 90,
               "trend_lookback_min": 30,
               # 240, not buffer's 270: the first 60s of a round are the
               # straddle's. The model path also refuses any round still
               # inside straddle_entry_window_s, because a trend widens this
               # window by trend_early_entry_s.
               "entry_window_start_s": 240, "entry_window_end_s": 30,
               # -- shared risk --
               # A floored 1.00 loss is a large share of a small bankroll and
               # a straddle's worst leg is 20%; 50% leaves room for both.
               "daily_loss_limit_pct": 0.50,
               # 30, not buffer's 6: every completed straddle pair settles
               # one leg as a LOSS (the pair still wins), so a few pairs
               # settling back to back read as a losing streak that is not.
               "max_consecutive_losses": 30, "max_rounds_per_day": 400,
               # Two slots per straddle round; four lets a second round open
               # while the first settles. 10% reserve funds both.
               "max_concurrent_positions": 4, "reserve_pct": 0.10,
               "entry_order_type": "MARKET", "exit_order_type": "NONE",
               "paper_start_bankroll": 10.0},
```

Before writing, open the `"buffer"` entry (lines ~249-319) and check every key listed there against this dict. Copy any buffer key missing above with buffer's value, except `entry_window_start_s`, `entry_window_end_s`, `daily_loss_limit_pct` and `paper_start_bankroll`.

- [ ] **Step 7: Run the new tests and the profile-wide config tests**

Run: `python -m unittest tests.test_config.TestHybridConfig tests.test_config.TestProfileDefaults tests.test_config.TestProfileRiskCoherence tests.test_config.TestStraddleConfig tests.test_config.TestScalpConfig -v`
Expected: all PASS.

Run: `python coherence.py`
Expected: no new error mentioning `hybrid_stop_loss_pct` or `hybrid_stop_disarm_s`.

- [ ] **Step 8: Commit**

```bash
git branch --show-current   # must print master
git add btc5m/config.py btc5m/profiles.py tests/support.py tests/test_config.py
git commit -m "Add the hybrid profile's settings and validation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Dispatch, and keeping the two layers from blocking each other

Two existing behaviours would silently break hybrid:
1. `_maybe_enter_straddle` writes `_seen[topic]` for every round past its 60s
   opening window, and `_maybe_enter_model` skips every round in `_seen`. So
   buffer would never trade.
2. A trend widens buffer's window to 240 + 90 = 330s, which is past the straddle's
   opening minute. So buffer could take a round before straddle has looked at it.

**Files:**
- Create: `btc5m/trader/hybrid.py`
- Modify: `btc5m/trader/core.py`: import, `Trader` bases, `_maybe_enter`
- Modify: `btc5m/trader/straddle.py`: the window-expiry `_seen` write in `_maybe_enter_straddle` (~line 408)
- Modify: `btc5m/trader/model_entry.py`: round loop in `_maybe_enter_model` (~line 50)
- Test: `tests/test_strategies.py`: new `TestHybridEntry`

**Interfaces:**
- Consumes: `Config.hybrid`, `hybrid_cfg`
- Produces: `HybridMixin._maybe_enter_hybrid(self, bankroll: float, mode: str) -> None`

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_strategies.py` and add `hybrid_cfg` to the support import.

```python
class TestHybridEntry(unittest.TestCase):

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def _trader(self, client, **kw):
        return build_trader(client, hybrid_cfg(db_path=self.db, **kw),
                            self.db)

    def test_dispatch_runs_straddle_then_model(self):
        t = self._trader(FakeClient([], [(0, 100_000.0)], {}, {}))
        calls = []
        t._maybe_enter_straddle = lambda *a: calls.append("straddle")
        t._maybe_enter_model = lambda *a: calls.append("model")
        t._maybe_enter_scalp = lambda *a: calls.append("scalp")
        t._maybe_enter(100.0, "PAPER")
        self.assertEqual(calls, ["straddle", "model"])

    def test_a_round_past_the_straddle_window_is_not_marked_seen(self):
        """
        The straddle path writes a round off once its opening minute has
        passed. In hybrid that write would also hide the round from buffer,
        which reads the same _seen, so buffer would never trade.
        """
        start = 1_700_000_000_000
        rnd = make_round(start_ms=start,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        books = {(1, Side.UP): [(0.70, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([rnd], [(start + 90_000, 100_000.0)], books, {})
        t = self._trader(client)
        t._maybe_enter_straddle(100.0, "PAPER")
        self.assertNotIn(1, t._seen)

    def test_the_straddle_profile_still_writes_that_round_off(self):
        start = 1_700_000_000_000
        rnd = make_round(start_ms=start,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        books = {(1, Side.UP): [(0.70, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([rnd], [(start + 90_000, 100_000.0)], books, {})
        t = build_trader(client, straddle_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(100.0, "PAPER")
        self.assertIn(1, t._seen)

    def test_buffer_does_not_enter_inside_the_straddle_minute(self):
        """
        entry_window_start_s is widened to the whole round so the window
        cannot be what refuses -- as a trend widening it would -- leaving
        the straddle-minute guard as the only thing that can. The same round
        30s later (90s in) must enter, which proves the fixture really is a
        buffer signal and the first assertion is not passing vacuously.
        """
        start = 1_700_000_000_000
        rnd = make_round(strike=65_000.0, start_ms=start, fee_bps=0,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        books = {(1, Side.UP): [(0.70, 1e6)], (1, Side.DOWN): [(0.75, 1e6)]}
        client = FakeClient([rnd], [(start + 30_000, 65_400.0),
                                    (start + 90_000, 65_400.0)], books, {})
        t = self._trader(client, entry_window_start_s=300)
        t._maybe_enter_model(100.0, "PAPER")
        self.assertEqual(t._positions, {})
        client.t = 1
        t._maybe_enter_model(100.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.UP), t._positions)

    def test_a_market_holding_a_straddle_leg_gets_no_buffer_entry(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=65_000.0, start_ms=start, fee_bps=0,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        books = {(1, Side.UP): [(0.30, 1e6)], (1, Side.DOWN): [(0.72, 1e6)]}
        client = FakeClient([rnd], [(start + 5_000, 65_000.0),
                                    (start + 120_000, 64_600.0)], books, {})
        t = self._trader(client)
        t._maybe_enter(100.0, "PAPER")          # opens the UP leg at 0.30
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])
        client.t = 1                             # DOWN now a buffer candidate
        t._maybe_enter(100.0, "PAPER")
        self.assertNotIn(("BTCUSDT", Side.DOWN),
                         {k for k, p in t._positions.items()
                          if p.signal.model_prob != 0.5})
```

The last test allows DOWN to arrive as a straddle *completion*. A straddle leg is
recorded with `model_prob=0.5`. It only rejects DOWN arriving as a buffer
entry.

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_strategies.TestHybridEntry -v`
Expected: `test_dispatch_runs_straddle_then_model` FAILs (the model path alone runs), and so do `test_a_round_past_the_straddle_window_is_not_marked_seen` and `test_buffer_does_not_enter_inside_the_straddle_minute` (its first assertion). If the straddle-minute test fails on its **second** assertion instead, the fixture is not producing a buffer signal. Adjust spot/book until the 90s entry happens with the guard absent before going further, or the test proves nothing.

- [ ] **Step 3: Create `btc5m/trader/hybrid.py`**

```python
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
        so a market holds one strategy's bet per round and never both. The
        bankroll is re-read between the two because the straddle may have
        just committed some of it.
        """
        self._maybe_enter_straddle(bankroll, mode)
        self._maybe_enter_model(bankroll, mode)
```

- [ ] **Step 4: Wire it into `core.py`**

Add the import beside the others:

```python
from btc5m.trader.hybrid import HybridMixin
```

Add `HybridMixin` to `Trader`'s bases (before `StraddleMixin`), and put the branch first in `_maybe_enter`:

```python
        if self._cfg.hybrid:
            self._maybe_enter_hybrid(bankroll, mode)
        elif self._cfg.straddle:
```

- [ ] **Step 5: Stop straddle writing rounds off under hybrid.** In `_maybe_enter_straddle` (`straddle.py` ~line 406), replace:

```python
            if (since_open > self._cfg.straddle_entry_window_s
                    or raw.seconds_remaining(now_ms) <= runway):
                self._seen[raw.topic_id] = raw.end_ms
                continue
```

with:

```python
            if (since_open > self._cfg.straddle_entry_window_s
                    or raw.seconds_remaining(now_ms) <= runway):
                # Hybrid hands this round to the buffer layer, which reads
                # the same _seen: writing it off here would mean buffer never
                # sees a round at all. A round a leg was OPENED on is still
                # written off, by _open_first_leg, and stays off to both.
                if not self._cfg.hybrid:
                    self._seen[raw.topic_id] = raw.end_ms
                continue
```

- [ ] **Step 6: Keep buffer out of the straddle minute.** In `_maybe_enter_model`, directly after the `if any(k[0] == raw.symbol for k in self._positions): continue` check, add:

```python
            # The first straddle_entry_window_s of a round are the straddle
            # layer's. A trend widens buffer's own window past 240s, so the
            # window setting alone cannot keep the two apart.
            if (self._cfg.hybrid and (now_ms - raw.start_ms) / 1000.0
                    <= self._cfg.straddle_entry_window_s):
                continue
```

- [ ] **Step 7: Run tests**

Run: `python -m unittest tests.test_strategies.TestHybridEntry tests.test_strategies.TestStraddleEntry -v`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git branch --show-current   # master
git add btc5m/trader/hybrid.py btc5m/trader/core.py btc5m/trader/straddle.py btc5m/trader/model_entry.py tests/test_strategies.py
git commit -m "Dispatch the hybrid profile: straddle first, then buffer

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Buffer stakes floored at $1

`assess` sizes through `kelly_stake(available, ...)`, which refuses the $1
round-up past `hard_max_stake_pct` (0.25 here) or 2x full Kelly. Separately,
`boosted_stake` caps a trend boost at those same limits. Once the stake is
floored, that cap would pull a $1 stake back *under* $1.

**Files:**
- Modify: `btc5m/sizing.py`: `kelly_stake` (~line 57-68), `boosted_stake` (~line 132-140)
- Test: `tests/test_pricing.py`: new `TestHybridStakeFloor`

**Interfaces:**
- Consumes: `Config.hybrid`
- Produces: `kelly_stake` returns `cfg.min_stake_usdt` for hybrid whenever `full_kelly > 0`, the capped stake is under the minimum, and `bankroll >= cfg.min_stake_usdt`. `boosted_stake` never returns less than its input `stake` for hybrid.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pricing.py`, importing `hybrid_cfg` from `tests.support`)

```python
class TestHybridStakeFloor(unittest.TestCase):
    """10% of a balance under 10.00 is under the 1.00 minimum; hybrid floors."""

    def test_a_small_balance_stakes_the_minimum(self):
        c = hybrid_cfg()
        # 6.00 x 10% = 0.60 < 1.00
        self.assertEqual(m.kelly_stake(6.0, 0.80, 0.65, c, 0), 1.0)

    def test_the_floor_ignores_the_hard_cap_and_the_two_kelly_limit(self):
        c = hybrid_cfg()
        # At 3.00, 1.00 is 33% -- past the 25% hard cap. The buffer profile
        # refuses this; hybrid is asked to take it.
        buffer = Config(api_key="k", api_secret="s", **m.PROFILES["buffer"])
        self.assertEqual(m.kelly_stake(3.0, 0.72, 0.65, buffer, 0), 0.0)
        self.assertEqual(m.kelly_stake(3.0, 0.72, 0.65, c, 0), 1.0)

    def test_no_edge_is_still_no_stake(self):
        c = hybrid_cfg()
        self.assertEqual(m.kelly_stake(6.0, 0.60, 0.65, c, 0), 0.0)

    def test_a_balance_under_the_minimum_stakes_nothing(self):
        c = hybrid_cfg()
        self.assertEqual(m.kelly_stake(0.90, 0.90, 0.65, c, 0), 0.0)

    def test_a_large_balance_is_sized_by_kelly_as_before(self):
        c = hybrid_cfg()
        self.assertGreater(m.kelly_stake(100.0, 0.80, 0.65, c, 0), 1.0)

    def test_a_trend_boost_never_shrinks_a_floored_stake(self):
        c = hybrid_cfg()
        boosted = m.boosted_stake(1.0, 3.0, 0.72, 0.65, c, 0)
        self.assertGreaterEqual(boosted, 1.0)
```

Check the `boosted_stake` argument order against `btc5m/sizing.py:114` before
running. The test assumes `(stake, bankroll, model_prob, price, cfg, fee_bps)`,
which is what `scale_in.py` passes.

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_pricing.TestHybridStakeFloor -v`
Expected: the floor-past-hard-cap test FAILs with `0.0 != 1.0`, and so does the boost test.

- [ ] **Step 3: Implement the floor in `kelly_stake`.** Replace the tail after `if stake >= cfg.min_stake_usdt: return stake`:

```python
    if cfg.hybrid:
        # Asked for: the hybrid profile trades a balance whose stake fraction
        # sizes under the venue minimum, at the minimum, even where that is
        # past the hard cap and 2x Kelly. The caller logs the over-bet.
        return cfg.min_stake_usdt if bankroll >= cfg.min_stake_usdt else 0.0

    if not cfg.round_up_to_minimum:
        return 0.0
```

(The remaining `forced_fraction` lines stay as they are.)

- [ ] **Step 4: Stop the boost from undercutting it.** In `boosted_stake`, replace the final `return`:

```python
    boosted = min(stake * cfg.trend_stake_multiple, ceiling)
    if cfg.hybrid:
        # The ceiling is the ruin bound on the BOOST. Below a floored stake
        # it would cut the order under the venue minimum, which is not a
        # smaller bet but no bet.
        return max(boosted, stake)
    return boosted
```

- [ ] **Step 5: Log the over-bet once per entry.** In `_maybe_enter_model` (`model_entry.py`), the existing `mult > 1.0` warning already fires for a floored stake ("Staking %.2fx the full-Kelly fraction because the venue minimum exceeds the Kelly size"). Extend its message for hybrid so it states the share of bankroll:

```python
            if mult is not None and mult > 1.0:
                LOG.warning("Staking %.2fx the full-Kelly fraction because the "
                            "venue minimum exceeds the Kelly size on a %.2f "
                            "bankroll%s", mult, bankroll,
                            f" (stake floored to {sig.stake_usdt:.2f}, "
                            f"{sig.stake_usdt / bankroll:.0%} of it)"
                            if self._cfg.hybrid else "")
```

- [ ] **Step 6: Run tests**

Run: `python -m unittest tests.test_pricing.TestHybridStakeFloor tests.test_pricing.TestSmallAccountSizing tests.test_config.TestProfileDefaults tests.test_trader.TestScaleIn tests.test_trader.TestScaleInSizing tests.test_trader.TestTrendBoost -v`
Expected: all PASS. `test_profiles_either_trade_a_small_balance_or_decline_it` probes at 6.64, where 1.00 is 15%, which is under the 25% cap, so it passes unchanged.

- [ ] **Step 7: Commit**

```bash
git branch --show-current   # master
git add btc5m/sizing.py btc5m/trader/model_entry.py tests/test_pricing.py
git commit -m "Floor hybrid buffer stakes at the venue minimum

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Straddle legs floored at $1

**Files:**
- Modify: `btc5m/sizing.py`: extract `straddle_completion_band` from `straddle_completion_stake` (~line 212-258)
- Modify: `btc5m/trader/straddle.py`: `per_side` in `_maybe_enter_straddle` (~365-384); the both-now split (~436-441); `_open_first_leg` (~134); `_complete_half_straddles` (~208-244)
- Test: `tests/test_strategies.py`: new `TestHybridStraddleFloor`; `tests/test_pricing.py`: band test

**Interfaces:**
- Produces: `straddle_completion_band(stake_open: float, price_open: float, price_other: float, fee_bps: int) -> tuple[float, float]` returning `(floor, ceiling)`. A second-leg stake `b` locks the round iff `floor < b < ceiling`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_pricing.py`:

```python
class TestStraddleCompletionBand(unittest.TestCase):

    def test_the_band_is_what_the_stake_function_tests_against(self):
        lo, hi = m.straddle_completion_band(1.0, 0.40, 0.45, 0)
        stake, guaranteed = m.straddle_completion_stake(1.0, 0.40, 0.45, 0,
                                                        10.0)
        self.assertEqual(guaranteed, lo < stake < hi)
        self.assertAlmostEqual(hi, 1.0 * 0.60 / 0.40)
```

In `tests/test_strategies.py`:

```python
class TestHybridStraddleFloor(unittest.TestCase):
    """20% of a balance under 5.00 is under the 1.00 minimum; hybrid floors."""

    START = 1_700_000_000_000

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def _round(self):
        return make_round(start_ms=self.START, fee_bps=0,
                          end_ms=self.START + m.DEFAULT_ROUND_SECONDS * 1000)

    def test_a_first_leg_on_4_00_stakes_the_minimum(self):
        books = {(1, Side.UP): [(0.35, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([self._round()], [(self.START + 5_000, 1.0)],
                            books, {})
        t = build_trader(client, hybrid_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(4.0, "PAPER")    # 20% = 0.80
        self.assertEqual(list(t._positions), [("BTCUSDT", Side.UP)])
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.UP)].committed_usdt, 1.0)

    def test_the_straddle_profile_still_refuses_that_balance(self):
        books = {(1, Side.UP): [(0.35, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([self._round()], [(self.START + 5_000, 1.0)],
                            books, {})
        t = build_trader(client, straddle_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(4.0, "PAPER")
        self.assertEqual(t._positions, {})

    def test_no_first_leg_without_the_money_for_its_partner(self):
        # 1.80 x 0.90 = 1.62 free: one leg fits, its partner does not.
        books = {(1, Side.UP): [(0.35, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([self._round()], [(self.START + 5_000, 1.0)],
                            books, {})
        t = build_trader(client, hybrid_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(1.80, "PAPER")
        self.assertEqual(t._positions, {})

    def test_a_both_now_pair_scales_its_cheap_leg_up_to_the_minimum(self):
        # 0.20 / 0.70 sums to 0.90: a lock. At 10.00 each leg is 2.00, and
        # splitting that 4.00 pair by payout puts UP at 0.89 -- under the
        # minimum -- so the pair is scaled to 4.50, where UP is 1.00.
        books = {(1, Side.UP): [(0.20, 10_000)],
                 (1, Side.DOWN): [(0.70, 10_000)]}
        client = FakeClient([self._round()], [(self.START + 5_000, 1.0)],
                            books, {})
        t = build_trader(client, hybrid_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(10.0, "PAPER")   # 9.00 free
        self.assertEqual(len(t._positions), 2)
        up = t._positions[("BTCUSDT", Side.UP)].committed_usdt
        down = t._positions[("BTCUSDT", Side.DOWN)].committed_usdt
        self.assertAlmostEqual(min(up, down), 1.0)
        for side, stake in ((Side.UP, up), (Side.DOWN, down)):
            price = 0.20 if side is Side.UP else 0.70
            self.assertGreater(stake / price, up + down)

    def test_a_cheap_completion_floors_to_the_minimum_when_it_still_locks(self):
        # Leg 1: 1.00 at 0.40. Leg 2 at 0.30: ideal 0.75, floored to 1.00.
        # Band for 0.40/0.30 is (0.43, 1.50): 1.00 locks, so it is taken.
        books = {(1, Side.UP): [(0.40, 10_000)],
                 (1, Side.DOWN): [(0.75, 10_000)]}
        client = FakeClient([self._round()],
                            [(self.START + 5_000, 1.0),
                             (self.START + 200_000, 1.0)], books, {})
        t = build_trader(client, hybrid_cfg(db_path=self.db), self.db)
        t._maybe_enter_straddle(4.0, "PAPER")
        books[(1, Side.UP)] = [(0.75, 10_000)]
        books[(1, Side.DOWN)] = [(0.30, 10_000)]
        client.t = 1
        t._maybe_enter_straddle(4.0, "PAPER")
        self.assertIn(("BTCUSDT", Side.DOWN), t._positions)
        self.assertAlmostEqual(
            t._positions[("BTCUSDT", Side.DOWN)].committed_usdt, 1.0)
```

If `_completion_is_worth_waiting_out` holds the 0.30 completion back at 100s
remaining, move the second spot-path timestamp later. For example, use
`START + 240_000` so 60s remain, which is above the 30s hedge deadline. The
assertion stays the same.

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_pricing.TestStraddleCompletionBand tests.test_strategies.TestHybridStraddleFloor -v`
Expected: `AttributeError: straddle_completion_band`; the hybrid floor tests FAIL.

- [ ] **Step 3: Extract the band in `btc5m/sizing.py`.** Add above `straddle_completion_stake`:

```python
def straddle_completion_band(stake_open: float, price_open: float,
                             price_other: float,
                             fee_bps: int) -> tuple[float, float]:
    """
    (floor, ceiling): a second-leg stake strictly between them locks the round.

    Below the floor the other leg's payout cannot cover the pair; above the
    ceiling the open leg's cannot. Strict at both ends, because at either
    edge a payout merely equals what the round cost. See
    straddle_completion_stake for the derivation.
    """
    be_open = breakeven_probability(price_open, fee_bps)
    be_other = breakeven_probability(price_other, fee_bps)
    floor = (math.inf if be_other >= 1.0
             else stake_open * be_other / (1.0 - be_other))
    ceiling = stake_open * (1.0 - be_open) / be_open
    return floor, ceiling
```

In `straddle_completion_stake`, replace the last five lines (the `# Strict bounds` comment through `return`) with:

```python
    floor, ceiling = straddle_completion_band(stake_open, price_open,
                                              price_other, fee_bps)
    return stake, floor < stake < ceiling
```

Export it: add `straddle_completion_band` to the `from btc5m.sizing import (...)` list in `btc_5m_predictor.py` (~line 128), and to the import in `straddle.py` line 14.

- [ ] **Step 4: Floor `per_side`.** In `_maybe_enter_straddle`, replace `per_side = bankroll * self._cfg.straddle_stake_pct` with:

```python
        per_side = bankroll * self._cfg.straddle_stake_pct
        if self._cfg.hybrid and per_side < self._cfg.min_stake_usdt:
            # Floored, not refused: see the hybrid profile. The warning below
            # can then only fire for the plain straddle profile.
            LOG.debug("Straddle leg %.2f (%.0f%% of %.2f) floored to the "
                      "%.2f minimum", per_side,
                      self._cfg.straddle_stake_pct * 100, bankroll,
                      self._cfg.min_stake_usdt)
            per_side = self._cfg.min_stake_usdt
```

- [ ] **Step 5: Scale a both-now pair up.** Directly after `stakes = dict(zip(... straddle_split(...)))` (~line 438), add:

```python
            if self._cfg.hybrid:
                cheap = min(stakes.values())
                if 0 < cheap < self._cfg.min_stake_usdt:
                    # The payout weighting put the cheap leg under the
                    # minimum. Scale the pair, not the leg -- scaling one leg
                    # breaks the equal payouts the split exists for.
                    scaled = total * self._cfg.min_stake_usdt / cheap
                    if scaled <= free:
                        total = scaled
                        stakes = dict(zip((Side.UP, Side.DOWN),
                                          straddle_split(total, legs[Side.UP],
                                                         legs[Side.DOWN],
                                                         raw.fee_bps)))
```

If `scaled > free`, the stakes stay as they were, and `_straddle_payouts_clear` refuses the pair ("straddle leg below the venue minimum"). That is the intended refusal.

- [ ] **Step 6: Refuse a first leg its partner cannot follow.** In `_open_first_leg`, replace:

```python
        stake = min(per_side, self._available(bankroll))
        if stake < self._cfg.min_stake_usdt:
            return False
```

with:

```python
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
```

- [ ] **Step 7: Floor the completion stake.** In `_complete_half_straddles`, directly after the `stake, guaranteed = straddle_completion_stake(...)` call, add:

```python
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
```

- [ ] **Step 8: Run tests**

Run: `python -m unittest tests.test_pricing.TestStraddleCompletionBand tests.test_strategies.TestHybridStraddleFloor tests.test_strategies.TestStraddleEntry tests.test_strategies.TestStraddleCompletionBar -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git branch --show-current   # master
git add btc5m/sizing.py btc5m/trader/straddle.py btc_5m_predictor.py tests/test_pricing.py tests/test_strategies.py
git commit -m "Floor hybrid straddle legs at the venue minimum

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Buffer entries record the shares the venue delivered

On 2026-09-14 stops were refused with `-9000`: the sale asked for more shares
than were held. Scalp and straddle legs now record `filledShareQty`. The model
path and scale-in do not, and every hybrid buffer stop sells through
`pos.held_shares`.

**Files:**
- Modify: `btc5m/trader/model_entry.py`: the live branch after `confirm_fill` (~line 205), and the `Position(...)` construction (~line 238)
- Modify: `btc5m/trader/scale_in.py`: after the top-up `confirm_fill` (~line 165), and the `replace(pos, ...)` at the end
- Test: `tests/test_trader.py`: new `TestDeliveredSharesOnModelEntries`

**Interfaces:**
- Consumes: `client.delivered_shares(order_id) -> float | None` (already on `PredictionClient` and `FakeClient`)
- Produces: `Position.shares` set on live model entries; summed on top-ups

- [ ] **Step 1: Write the failing tests** (append to `tests/test_trader.py`)

```python
class TestDeliveredSharesOnModelEntries(unittest.TestCase):
    """A buffer stop sells pos.held_shares; it has to be the venue's count."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def test_a_live_model_entry_records_the_delivered_shares(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})
        client.delivered_shares = lambda order_id: 3.21
        t = build_trader(client, cfg(db_path=self.db, live=True), self.db)
        client.t = 1
        t._maybe_enter(100.0, "LIVE")
        self.assertIsNotNone(t._position)
        self.assertAlmostEqual(t._position.shares, 3.21)

    def test_paper_entries_still_record_no_count(self):
        start = 1_700_000_000_000
        rnd = make_round(strike=None, start_ms=start,
                         end_ms=start + (m.DEFAULT_ROUND_SECONDS * 1000))
        path = [(start, 100_000.0), (start + 240_000, 100_400.0)]
        books = {(1, Side.UP): [(0.55, 10_000)]}
        client = FakeClient([rnd], path, books, {})
        t = build_trader(client, cfg(db_path=self.db), self.db)
        client.t = 1
        t._maybe_enter(100.0, "PAPER")
        self.assertIsNone(t._position.shares)

    def test_a_live_top_up_adds_its_delivered_shares(self):
        settings = dict(m.PROFILES["buffer"])
        c = cfg(db_path=self.db, live=True, **settings)
        start = 1_700_000_000_000
        rnd = make_round(strike=65_000.0, start_ms=start, fee_bps=0,
                         end_ms=start + m.DEFAULT_ROUND_SECONDS * 1000)
        path = [(start, 65_000.0), (start + 120_000, 65_260.0)]
        book = {(1, Side.UP): [(0.72, 1e6)], (1, Side.DOWN): [(0.75, 1e6)]}
        client = FakeClient([rnd], path, book, {})
        client.get_quote = lambda r, p: m.Quote("q", 0.72, p.amount / 0.72,
                                                0.0, 0.0)
        client.delivered_shares = lambda order_id: 2.50
        t = build_trader(client, c, self.db)
        sig = Signal(Side.UP, 0.90, 0.68, 0.02, 2.0, 120.0, 1.8)
        tid = t._journal.record("LIVE", rnd, sig, 65_000, 0.5, 100.0)
        t._position = Position(tid, rnd, sig, 2.0, 1, shares=2.90)
        client.t = 1
        t._maybe_scale_in(100.0)
        self.assertEqual(t._position.tranches, 2)
        self.assertAlmostEqual(t._position.shares, 2.90 + 2.50)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_trader.TestDeliveredSharesOnModelEntries -v`
Expected: first test FAILs (`None != 3.21`); the top-up test FAILs (`2.90 != 5.40`). If the top-up test fails earlier because no top-up happens (`tranches` stays 1), copy the working fixture from `TestScaleIn._setup` and `test_tops_up_when_the_round_moves_further_ahead` exactly, and change only `live=True`, the quote, and `delivered_shares`.

- [ ] **Step 3: Record shares in `model_entry.py`.** Before the `if self._live:` block, initialise `shares = None`. Inside the live branch, directly after `LOG.info("Order %s filled at %.4f for %.4f shares", ...)`, add:

```python
                # The venue's count, not the quote's. Holdings are kept to two
                # decimals and the buy's fee comes out of the shares, so a
                # sale sized from the quote was refused as exceeding what is
                # held -- which is the one thing a stop cannot survive.
                shares = self._client.delivered_shares(order_id)
```

Change the position construction to pass it:

```python
            self._positions[(rnd.symbol, sig.side)] = Position(
                tid, rnd, sig, sig.stake_usdt, 1, shares=shares)
```

- [ ] **Step 4: Sum shares in `scale_in.py`.** Initialise `delivered = None` before `if self._live:`. In the live branch after `confirm_fill` succeeds, set `delivered = self._client.delivered_shares(topup_order)`. Change the final `replace(pos, ...)` to also pass:

```python
            shares=(None if pos.shares is None
                    else pos.shares + (delivered if delivered is not None
                                       else topup / avg)),
```

- [ ] **Step 5: Run tests**

Run: `python -m unittest tests.test_trader.TestDeliveredSharesOnModelEntries tests.test_trader.TestLiveQuoteGate tests.test_trader.TestScaleIn tests.test_trader.TestPartialFills -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git branch --show-current   # master
git add btc5m/trader/model_entry.py btc5m/trader/scale_in.py tests/test_trader.py
git commit -m "Record delivered shares on model entries and top-ups

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The hybrid stop

**Files:**
- Modify: `btc5m/trader/hybrid.py`: add `_sync_hybrid_stops`
- Modify: `btc5m/trader/core.py`: `self._hybrid_stopped: set[int] = set()` in `__init__` (beside `_brackets`); call `self._sync_hybrid_stops()` immediately before `self._check_stops()` in `run` (~line 303)
- Modify: `btc5m/trader/scalp.py`: `_check_stops` (~506-549)
- Test: `tests/test_strategies.py`: new `TestHybridStops`

**Interfaces:**
- Consumes: `Bracket(entry_price, tp_price, stop_price)` from `btc5m.domain`; `_sell_now(pos, reason) -> bool`; `Config.hybrid_stop_loss_pct`, `Config.hybrid_stop_disarm_s`
- Produces: `HybridMixin._sync_hybrid_stops(self) -> None`; `Trader._hybrid_stopped: set[int]` (trade ids whose stop has fired once)

Rules `_sync_hybrid_stops` enforces, every pass:
1. Hybrid off: do nothing.
2. Forget stopped trade ids whose positions are gone.
3. For each position: if its symbol holds **both** sides, it is a locked pair. Drop its bracket.
4. Otherwise, if its trade id is in `_hybrid_stopped`, skip it. The stop fires once; a second pass must not sell a sale already under way.
5. Otherwise arm or re-arm `Bracket(entry, inf, round_price(entry x (1 - pct)))`, where `entry = pos.signal.fill_price`. Re-arm when the existing bracket's `entry_price` differs, which is what a scale-in top-up does to `fill_price`.

- [ ] **Step 1: Write the failing tests**

```python
class TestHybridStops(unittest.TestCase):

    START = 1_700_000_000_000

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        _close_journals(self)
        os.unlink(self.db)

    def _trader(self, bid, now_offset_s=100, **kw):
        rnd = make_round(start_ms=self.START,
                         end_ms=self.START + m.DEFAULT_ROUND_SECONDS * 1000)
        now = self.START + now_offset_s * 1000
        client = ScalpClient([rnd], [(now, 100_000.0)], {}, {},
                             bids={(1, Side.UP): [(bid, 10_000)],
                                   (1, Side.DOWN): [(bid, 10_000)]})
        t = build_trader(client, hybrid_cfg(db_path=self.db, **kw), self.db)
        return t, client, rnd

    def _hold(self, t, rnd, side, price, prob=0.80, stake=1.0):
        sig = Signal(side, prob, price, 0.02, stake, 200.0)
        tid = t._journal.record("PAPER", rnd, sig, 0.0, 0.0, 10.0)
        t._positions[("BTCUSDT", side)] = Position(tid, rnd, sig, stake, 1)
        return ("BTCUSDT", side)

    def _sold(self, t):
        return [p for p in t._pending.values()
                if p.plan.action is m.Action.SELL]

    def test_a_buffer_position_is_stopped_25_percent_under_entry(self):
        t, _, rnd = self._trader(bid=0.52)
        key = self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        self.assertAlmostEqual(t._brackets[key].stop_price, 0.525, places=3)
        t._check_stops()
        self.assertEqual(len(self._sold(t)), 1)

    def test_a_bid_above_the_stop_sells_nothing(self):
        t, _, rnd = self._trader(bid=0.60)
        self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        t._check_stops()
        self.assertEqual(self._sold(t), [])

    def test_there_is_no_take_profit(self):
        t, _, rnd = self._trader(bid=0.99)
        self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        t._check_stops()
        self.assertEqual(self._sold(t), [])

    def test_an_unpaired_straddle_leg_is_stopped(self):
        t, _, rnd = self._trader(bid=0.25)
        key = self._hold(t, rnd, Side.UP, 0.35, prob=0.5)
        t._sync_hybrid_stops()
        self.assertIn(key, t._brackets)
        t._check_stops()
        self.assertEqual(len(self._sold(t)), 1)

    def test_a_completed_pair_is_never_stopped(self):
        t, _, rnd = self._trader(bid=0.05)
        up = self._hold(t, rnd, Side.UP, 0.35, prob=0.5)
        t._sync_hybrid_stops()
        self.assertIn(up, t._brackets)           # armed while unpaired
        self._hold(t, rnd, Side.DOWN, 0.30, prob=0.5)
        t._sync_hybrid_stops()
        self.assertEqual(t._brackets, {})        # disarmed once paired
        t._check_stops()
        self.assertEqual(self._sold(t), [])

    def test_no_stop_fires_inside_the_disarm_window(self):
        # 260s into a 300s round: 40s left, inside the 45s window.
        t, _, rnd = self._trader(bid=0.10, now_offset_s=260)
        self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        t._check_stops()
        self.assertEqual(self._sold(t), [])

    def test_a_fired_stop_is_not_re_armed(self):
        t, _, rnd = self._trader(bid=0.50)
        self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        t._check_stops()
        t._sync_hybrid_stops()
        t._check_stops()
        self.assertEqual(len(self._sold(t)), 1)

    def test_a_top_up_re_arms_from_the_blended_price(self):
        t, _, rnd = self._trader(bid=0.60)
        key = self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        pos = t._positions[key]
        t._positions[key] = replace(
            pos, signal=replace(pos.signal, fill_price=0.74),
            committed_usdt=2.0, tranches=2)
        t._sync_hybrid_stops()
        self.assertAlmostEqual(t._brackets[key].entry_price, 0.74)
        self.assertAlmostEqual(t._brackets[key].stop_price, 0.555, places=3)

    def test_the_scalp_profile_arms_nothing_through_this_path(self):
        rnd = make_round()
        client = ScalpClient([rnd], [(rnd.end_ms - 200_000, 1.0)], {}, {})
        t = build_trader(client, scalp_cfg(db_path=self.db), self.db)
        self._hold(t, rnd, Side.UP, 0.70)
        t._sync_hybrid_stops()
        self.assertEqual(t._brackets, {})
```

Add `from dataclasses import replace` to the top of `tests/test_strategies.py` if it is not there.

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_strategies.TestHybridStops -v`
Expected: `AttributeError: ... '_sync_hybrid_stops'`.

- [ ] **Step 3: Add state in `core.py` `__init__`** (directly after `self._brackets` is declared)

```python
        # Hybrid: trade ids whose stop has already fired. A stop sells once;
        # re-arming it on the next pass would send a second sale of shares
        # the first one is still selling.
        self._hybrid_stopped: set[int] = set()
```

And in `run`, immediately before `self._check_stops()`:

```python
                    self._sync_hybrid_stops()
```

- [ ] **Step 4: Implement `_sync_hybrid_stops` in `hybrid.py`**

```python
import math

from btc5m.domain import Bracket
```

(at module top), and in `HybridMixin`:

```python
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
```

- [ ] **Step 5: Let `_check_stops` run for hybrid, with the disarm window.** In `scalp.py`, replace:

```python
        if not self._cfg.scalp:
            return
```

with:

```python
        if not (self._cfg.scalp or self._cfg.hybrid):
            return
        now_ms = self._client.now_ms()
```

Inside the loop, directly after the `if pos is None: ... continue` block, add:

```python
            if (self._cfg.hybrid and pos.rnd.seconds_remaining(now_ms)
                    <= self._cfg.hybrid_stop_disarm_s):
                # The last seconds of a round have the thinnest book; a sale
                # there turns a coin flip into a certain loss. The bracket
                # stays so nothing re-arms it, and the position settles.
                continue
```

And directly before `self._brackets.pop(key, None)` / `self._sell_now(pos, leg)` at the end of the loop body, add:

```python
            if self._cfg.hybrid:
                self._hybrid_stopped.add(pos.trade_id)
```

- [ ] **Step 6: Run tests**

Run: `python -m unittest tests.test_strategies.TestHybridStops tests.test_strategies.TestScalpStops tests.test_strategies.TestScalpFlatten tests.test_trader.TestLoopSurvivesUnexpectedFailures -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git branch --show-current   # master
git add btc5m/trader/hybrid.py btc5m/trader/core.py btc5m/trader/scalp.py tests/test_strategies.py
git commit -m "Put a stop-loss under every hybrid position that can still lose

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Preflight reports hybrid sizing

`balance_check` inside `preflight` is a closure that is hard to test. Put the
hybrid wording in a pure function and call it from there.

**Files:**
- Modify: `btc5m/probes.py`: new module-level `hybrid_balance_notes`; a `cfg.hybrid` branch in `balance_check` before `elif cfg.scalp:`
- Test: `tests/test_cli.py`: new `TestHybridBalanceNotes`

**Interfaces:**
- Produces: `hybrid_balance_notes(cfg: Config, bal: float) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
class TestHybridBalanceNotes(unittest.TestCase):

    def test_a_small_balance_is_reported_as_floored_not_untradeable(self):
        from btc5m.probes import hybrid_balance_notes
        notes = "\n".join(hybrid_balance_notes(hybrid_cfg(), 4.0))
        self.assertIn("straddle legs 1.00", notes)
        self.assertIn("buffer stakes 1.00", notes)
        self.assertIn("floored", notes)
        self.assertNotIn("UNTRADEABLE", notes)

    def test_a_balance_that_cannot_fund_a_straddle_pair_says_so(self):
        from btc5m.probes import hybrid_balance_notes
        notes = "\n".join(hybrid_balance_notes(hybrid_cfg(), 1.80))
        self.assertIn("no straddle", notes)
        self.assertIn("buffer stakes 1.00", notes)

    def test_a_large_balance_reports_the_fractions(self):
        from btc5m.probes import hybrid_balance_notes
        notes = "\n".join(hybrid_balance_notes(hybrid_cfg(), 50.0))
        self.assertIn("straddle legs 10.00", notes)
        self.assertIn("buffer stakes up to 5.00", notes)
```

(import `hybrid_cfg` from `tests.support`)

- [ ] **Step 2: Run to verify failure**

Run: `python -m unittest tests.test_cli.TestHybridBalanceNotes -v`
Expected: `ImportError: cannot import name 'hybrid_balance_notes'`.

- [ ] **Step 3: Implement in `probes.py`** (module level, above `preflight`)

```python
def hybrid_balance_notes(cfg: Config, bal: float) -> list[str]:
    """
    What the hybrid profile will stake on this balance, and what it cannot.

    Neither layer calls the Kelly probe's refusal path -- both floor at the
    venue minimum -- so the generic probe would call a small hybrid account
    untradeable when it is not.
    """
    minimum = cfg.min_stake_usdt
    free = bal * (1.0 - cfg.reserve_pct)
    leg = bal * cfg.straddle_stake_pct
    cap = bal * cfg.max_stake_pct
    notes = []
    if free >= 2 * minimum:
        notes.append(f"  -> straddle legs {max(leg, minimum):.2f}"
                     + (f" (floored from {leg:.2f})" if leg < minimum else "")
                     + f"; {free:.2f} free after the {cfg.reserve_pct:.0%} "
                     f"reserve")
    else:
        notes.append(f"  <-- no straddle: {free:.2f} free cannot fund a leg "
                     f"and its partner at the {minimum:.2f} minimum")
    if free >= minimum:
        if cap < minimum:
            notes.append(f"  -> buffer stakes {minimum:.2f}, floored from "
                         f"{cap:.2f} ({minimum / bal:.0%} of bankroll, past "
                         f"the Kelly limits by design)")
        else:
            notes.append(f"  -> buffer stakes up to {cap:.2f} "
                         f"({cfg.max_stake_pct:.0%} of bankroll, Kelly-sized)")
    else:
        notes.append(f"  <-- no buffer entry: {free:.2f} free is under the "
                     f"{minimum:.2f} minimum")
    return notes
```

In `balance_check`, insert before `elif cfg.scalp:`:

```python
        elif cfg.hybrid:
            notes.extend(hybrid_balance_notes(cfg, bal))
```

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_cli.TestHybridBalanceNotes tests.test_cli.TestPreflightGate -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # master
git add btc5m/probes.py tests/test_cli.py
git commit -m "Report hybrid sizing in preflight

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Docs, deploy whitelist, and the Linux gate

**Files:**
- Modify: `README.md`: profile table (~line 226) and prose after `**lastminute**` (~line 262)
- Modify: `deploy/aws/deploy.sh:61`

- [ ] **Step 1: README table row** (after the `lastminute` row)

```markdown
| `hybrid` | straddle, else 0.55–0.80 | 20% per leg / 10%, floored at $1 | ≥0.75σ | 60s from open, then 240–30s | $10 |
```

- [ ] **Step 2: README prose** (after the `lastminute` paragraph)

```markdown
**`hybrid`** layers three profiles in one round. The first minute is
`straddle`'s, unchanged. A market with no leg after that is offered to
`buffer`'s gates, unchanged. Anything open that can still lose, whether a buffer
position or a first leg whose partner never came, carries a stop 25% under its
executed price, disarmed in the last 45 seconds. A completed pair is never
stopped. There is no take-profit. Both stakes floor at the $1 minimum instead of
refusing a small balance, which at $3 means betting a third of it. See
`docs/superpowers/specs/2026-09-16-hybrid-profile-design.md`.
```

- [ ] **Step 3: deploy.sh whitelist**

```bash
  scalp|straddle|last_minute|model|buffer|hybrid) ;;
```

- [ ] **Step 4: Local checks**

Run: `python coherence.py`
Expected: no error lines that are new since `7e11faf`.

Run: `python -m unittest tests.test_config tests.test_strategies.TestHybridEntry tests.test_strategies.TestHybridStraddleFloor tests.test_strategies.TestHybridStops tests.test_pricing tests.test_source_rules.TestCoherenceCorpus tests.test_deployment.TestDefaultProfileCoherence -v`
Expected: all PASS. The 8 shell-script failures documented as the Windows floor are the only acceptable failures, and they are not in these modules.

- [ ] **Step 5: Commit**

```bash
git branch --show-current   # master
git add README.md deploy/aws/deploy.sh
git commit -m "Document the hybrid profile and allow deploying it

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: WSL gate** (the real build gate; about 5 minutes)

```bash
wsl -d Ubuntu -- bash -lc 'cd ~/pt-src && git pull /mnt/e/Users/HP/Documents/Projects/Prediction\ Trading master && rm -rf ~/pt-image && mkdir ~/pt-image && cp *.py verify.sh entrypoint.sh requirements.txt ~/pt-image/ && cp -r btc5m tests ~/pt-image/ && cd ~/pt-image && export PATH=~/.venvs/pt/bin:$PATH TMPDIR=/dev/shm && BINANCE_API_KEY=build BINANCE_API_SECRET=build ./verify.sh'
```

Expected: `verify.sh` exits 0. On failure, fix on master, commit, and re-run. Do not push until it is green.

- [ ] **Step 7: Stop here.** Pushing, regenerating the EFS `config.json` (it embeds `PROFILES`), and the shadow session (`TRADING_MODE=shadow PROFILE=hybrid`) are the user's calls. Report the gate result and ask.
