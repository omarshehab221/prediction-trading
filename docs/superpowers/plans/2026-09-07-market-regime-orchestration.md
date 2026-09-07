# Market Regime Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot read which of three market states it is in and choose the strategy that fits, instead of running one strategy chosen at startup for the whole session.

**Architecture:** A detector classifies the last ~24 five-minute rounds against the ~100 behind them, using the 1m closes already fetched for volatility. A `PLAYBOOKS` table maps each state to an ordered list of (profile, overlay) entries; the first that opens a position wins the round. Governance limits (`daily_loss_limit_pct`, `max_consecutive_losses`, `max_rounds_per_day`) are pinned for the session by `ADAPTIVE_ENVELOPE` so switching strategies cannot move a budget accounted against the day.

**Tech Stack:** Python 3, stdlib only (`math`, `statistics`, `itertools`, `dataclasses`), `requests` for HTTP, `unittest` for tests. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-market-regime-orchestration-design.md`

## Global Constraints

- All production code goes in `btc_5m_predictor.py`. All tests go in `test_btc_5m.py`. The repo keeps one large module plus standalone tools; do not restructure it.
- The only new file is `backtest_regime.py`, a standalone tool matching the existing pattern of `flow.py`, `fuzz.py`, `conformance.py`.
- Standard library only. No numpy, no pandas.
- Tests run with `python -m unittest test_btc_5m -v`. Every task's tests must pass before commit.
- `Config` is a frozen dataclass whose `__post_init__` validates. Any new field needs its validation written in the same task.
- No new API calls in the trading loop. The detector is fed from the closes `VolatilityEstimator.sigma_annual` already fetches.
- Regime names are the exact strings `"FLAT"`, `"BIASED"`, `"SWINGY"`, plus `"UNKNOWN"` for the uncommitted initial state. No other spellings.
- Governance fields, pinned and never overridable by a playbook overlay: `daily_loss_limit_pct`, `max_consecutive_losses`, `max_rounds_per_day`.
- Commit after every task with the message given in that task's final step.

---

### Task 1: Round shape measurement

The primitive everything else reads: what one 5-minute round's price path looked like, measured against the price it opened at.

**Files:**
- Modify: `btc_5m_predictor.py` — insert after the `Trend` dataclass (ends at line 1382), before `class Signal`
- Test: `test_btc_5m.py` — append a new `TestRoundShape` class

**Interfaces:**
- Consumes: nothing
- Produces: `RoundShape` frozen dataclass with float fields `travel`, `net`, `straightness`, `terminal` and int field `crossings`; module function `measure_round_shape(closes: Sequence[float], sigma_block: float) -> RoundShape`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestRoundShape(unittest.TestCase):
    """One round's path, measured against the price it opened at."""

    def test_straight_ramp_is_perfectly_efficient(self):
        # Never turns around, so distance travelled == net displacement.
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        shape = m.measure_round_shape(closes, sigma_block=0.01)
        self.assertAlmostEqual(shape.straightness, 1.0, places=6)
        self.assertEqual(shape.crossings, 0)
        self.assertGreater(shape.terminal, 0.0)
        self.assertGreater(shape.net, 0.0)

    def test_sawtooth_across_the_open_crosses_repeatedly(self):
        # Above, below, above, below: four sign flips after the open.
        closes = [100.0, 101.0, 99.0, 101.0, 99.0, 101.0]
        shape = m.measure_round_shape(closes, sigma_block=0.01)
        self.assertEqual(shape.crossings, 4)
        self.assertLess(shape.straightness, 0.2)
        self.assertGreater(shape.travel, 0.0)

    def test_flat_path_travels_almost_nothing(self):
        closes = [100.0, 100.01, 100.0, 99.99, 100.0, 100.01]
        flat = m.measure_round_shape(closes, sigma_block=0.01)
        ramp = m.measure_round_shape([100.0, 101.0, 102.0, 103.0, 104.0,
                                      105.0], sigma_block=0.01)
        self.assertLess(flat.travel, ramp.travel)
        self.assertLess(flat.terminal, ramp.terminal)

    def test_terminal_is_the_absolute_net(self):
        closes = [100.0, 99.0, 98.0, 97.0, 96.0, 95.0]
        shape = m.measure_round_shape(closes, sigma_block=0.01)
        self.assertLess(shape.net, 0.0)
        self.assertAlmostEqual(shape.terminal, abs(shape.net), places=9)

    def test_degenerate_inputs_return_a_zero_shape(self):
        for closes, sigma in (([], 0.01), ([100.0], 0.01),
                              ([100.0, 101.0], 0.0),
                              ([0.0, 101.0], 0.01),
                              ([-5.0, 101.0], 0.01)):
            shape = m.measure_round_shape(closes, sigma)
            self.assertEqual(shape.travel, 0.0)
            self.assertEqual(shape.crossings, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestRoundShape -v`
Expected: FAIL with `AttributeError: module 'btc_5m_predictor' has no attribute 'measure_round_shape'`

- [ ] **Step 3: Write the implementation**

Insert into `btc_5m_predictor.py` immediately after the `Trend` class:

```python
@dataclass(frozen=True)
class RoundShape:
    """
    What one round's price path did, relative to the price it opened at.

    The opening price is the strike proxy, and that is not an approximation
    of convenience: the venue publishes variantData.startPrice, and it is
    the market's price at the moment the round opened. Measuring against it
    is measuring against the thing the round actually settles on.

    Every size is divided by one block's worth of noise, so "far" means far
    for this hour rather than a fixed number of basis points.
    """

    travel: float = 0.0        # distance walked inside the round / sigma
    net: float = 0.0           # signed end-to-end displacement / sigma
    straightness: float = 0.0  # |net| / travel, in [0, 1]
    terminal: float = 0.0      # |net|, i.e. how far from strike it finished
    crossings: int = 0         # sign flips of (close - strike) after the open


def measure_round_shape(closes: Sequence[float],
                        sigma_block: float) -> RoundShape:
    """
    Measure one round's path. `closes[0]` is the open, and is the strike.

    Returns a zero shape rather than raising on degenerate input. A single
    bad close in a kline feed must not be able to take the trading loop
    down, and "this round told us nothing" is the honest reading of it.
    """
    if len(closes) < 2 or sigma_block <= 0:
        return RoundShape()
    strike = closes[0]
    if strike <= 0 or any(c <= 0 for c in closes):
        return RoundShape()

    steps = [math.log(b / a) for a, b in itertools.pairwise(closes)]
    travelled = sum(abs(s) for s in steps)
    if travelled <= 0:
        return RoundShape()

    net = math.log(closes[-1] / strike)
    # Only the samples AFTER the open have a side. The open is the strike,
    # so counting it would score every round as starting on some side.
    signs = [1 if c > strike else -1 for c in closes[1:] if c != strike]
    crossings = sum(1 for a, b in itertools.pairwise(signs) if a != b)

    return RoundShape(travel=travelled / sigma_block,
                      net=net / sigma_block,
                      straightness=abs(net) / travelled,
                      terminal=abs(net) / sigma_block,
                      crossings=crossings)
```

Confirm `Sequence` is imported. If `from collections.abc import Sequence` is absent near the top of the file, add it to the existing `collections.abc` import line, or add the import.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestRoundShape -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Run the whole suite for regressions**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Measure what one round's path did, against the price it opened at"
```

---

### Task 2: Regime classification

Turn a window of round shapes into one of three labels, by comparing recent rounds against the baseline behind them.

**Files:**
- Modify: `btc_5m_predictor.py` — `Config` (add fields near `trend_lookback_min`, line 341; add validation in `__post_init__` near line 625) and a new module function after `measure_round_shape`
- Test: `test_btc_5m.py` — append `TestRegimeClassification`

**Interfaces:**
- Consumes: `RoundShape`, `measure_round_shape` from Task 1
- Produces: `classify_regime(recent: Sequence[RoundShape], baseline: Sequence[RoundShape], cfg: Config) -> tuple[str, dict[str, float]]` returning a label in `{"FLAT", "BIASED", "SWINGY", ""}` and the measurements behind it. `""` means no rule matched.

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
def _shapes(n, *, travel, straightness, terminal, crossings):
    """n identical RoundShapes, for classifier tests."""
    return [m.RoundShape(travel=travel, net=terminal,
                         straightness=straightness, terminal=terminal,
                         crossings=crossings) for _ in range(n)]


class TestRegimeClassification(unittest.TestCase):

    def setUp(self):
        self.cfg = m.Config(api_key="k", api_secret="s")
        # A neutral baseline: middling travel, middling terminal.
        self.baseline = _shapes(100, travel=2.0, straightness=0.5,
                                terminal=1.0, crossings=1)

    def test_quiet_rounds_near_the_strike_are_flat(self):
        recent = _shapes(24, travel=0.8, straightness=0.5, terminal=0.3,
                         crossings=1)
        label, measures = m.classify_regime(recent, self.baseline, self.cfg)
        self.assertEqual(label, "FLAT")
        self.assertLess(measures["travel_ratio"], 0.6)

    def test_straight_rounds_ending_far_out_are_biased(self):
        recent = _shapes(24, travel=3.0, straightness=0.9, terminal=2.5,
                         crossings=0)
        label, _ = m.classify_regime(recent, self.baseline, self.cfg)
        self.assertEqual(label, "BIASED")

    def test_wandering_rounds_that_end_nowhere_are_swingy(self):
        recent = _shapes(24, travel=4.0, straightness=0.2, terminal=0.9,
                         crossings=3)
        label, _ = m.classify_regime(recent, self.baseline, self.cfg)
        self.assertEqual(label, "SWINGY")

    def test_unmatched_window_returns_no_label(self):
        # Straight but not far out, few crossings: matches no rule.
        recent = _shapes(24, travel=2.0, straightness=0.5, terminal=1.0,
                         crossings=1)
        label, measures = m.classify_regime(recent, self.baseline, self.cfg)
        self.assertEqual(label, "")
        self.assertIn("straightness", measures)

    def test_too_few_rounds_returns_no_label(self):
        recent = _shapes(3, travel=0.8, straightness=0.5, terminal=0.3,
                         crossings=1)
        label, measures = m.classify_regime(recent, self.baseline, self.cfg)
        self.assertEqual(label, "")
        self.assertEqual(measures, {})

    def test_empty_baseline_returns_no_label(self):
        recent = _shapes(24, travel=0.8, straightness=0.5, terminal=0.3,
                         crossings=1)
        label, _ = m.classify_regime(recent, [], self.cfg)
        self.assertEqual(label, "")

    def test_labels_are_mutually_exclusive_over_a_sweep(self):
        # No input may satisfy two rules at once; that is what makes the
        # order of the checks irrelevant to the answer.
        for travel in (0.5, 1.5, 3.0, 5.0):
            for straight in (0.1, 0.35, 0.5, 0.6, 0.95):
                for terminal in (0.2, 0.9, 1.5, 3.0):
                    for crossings in (0, 1, 2, 4):
                        recent = _shapes(24, travel=travel,
                                         straightness=straight,
                                         terminal=terminal,
                                         crossings=crossings)
                        label, _ = m.classify_regime(recent, self.baseline,
                                                     self.cfg)
                        self.assertIn(label, ("", "FLAT", "BIASED", "SWINGY"))

    def test_thresholds_are_ratios_so_a_vol_shift_does_not_relabel(self):
        # Double every size in both windows: the label must not change.
        recent = _shapes(24, travel=0.8, straightness=0.5, terminal=0.3,
                         crossings=1)
        loud_recent = _shapes(24, travel=1.6, straightness=0.5, terminal=0.6,
                              crossings=1)
        loud_baseline = _shapes(100, travel=4.0, straightness=0.5,
                                terminal=2.0, crossings=1)
        quiet, _ = m.classify_regime(recent, self.baseline, self.cfg)
        loud, _ = m.classify_regime(loud_recent, loud_baseline, self.cfg)
        self.assertEqual(quiet, loud)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestRegimeClassification -v`
Expected: FAIL with `AttributeError: module 'btc_5m_predictor' has no attribute 'classify_regime'`

- [ ] **Step 3: Add the Config fields**

In `btc_5m_predictor.py`, immediately after `trend_lookback_min: int = 30` (line 341), insert:

```python
    # --- Market regime ----------------------------------------------------
    # Which of three states the market is in, read from the rounds that have
    # already happened. Nothing here forecasts: the detector reports how the
    # recent rounds differ from the ones before them, and "entering X" means
    # the evidence has moved and has not yet settled.
    #
    # How many recent rounds are the reading, and how many behind them are
    # the baseline it is compared against. The baseline is what makes
    # "diverging" a measurement -- without it every threshold would be a
    # hardcoded BTC number that stops meaning anything when volatility
    # shifts.
    regime_recent_rounds: int = 24
    regime_min_rounds: int = 8
    # Readings a candidate state must hold before it is committed. One
    # reading per round, so 3 is about 15 minutes of agreement: short enough
    # to catch a real shift inside a 1-2 hour state, long enough that a
    # single odd round cannot swap the strategy.
    regime_commit_readings: int = 3
    # FLAT: barely moved, and died near the strike.
    regime_flat_travel_ratio: float = 0.60
    regime_flat_terminal_ratio: float = 0.60
    # BIASED: picked a side, went, stayed.
    regime_biased_straightness: float = 0.55
    regime_biased_terminal_ratio: float = 1.20
    regime_biased_max_crossings: float = 0.5
    # SWINGY: covered ground and ended nowhere.
    regime_swingy_min_crossings: float = 1.2
    regime_swingy_straightness: float = 0.40
    regime_swingy_travel_ratio: float = 0.80
```

- [ ] **Step 4: Add the Config validation**

In `Config.__post_init__`, immediately after the `trend_lookback_min` check block that ends near line 630, insert:

```python
        if self.regime_recent_rounds < 2:
            raise ValueError("regime_recent_rounds must be at least 2")
        if self.regime_min_rounds < 2:
            raise ValueError("regime_min_rounds must be at least 2")
        if self.regime_min_rounds > self.regime_recent_rounds:
            raise ValueError(
                "regime_min_rounds must not exceed regime_recent_rounds")
        if self.regime_commit_readings < 1:
            raise ValueError("regime_commit_readings must be at least 1")
        # The two travel gates must not overlap, or one window could satisfy
        # both FLAT and SWINGY and the label would depend on check order.
        if self.regime_flat_travel_ratio >= self.regime_swingy_travel_ratio:
            raise ValueError(
                "regime_flat_travel_ratio must be below "
                "regime_swingy_travel_ratio; overlapping bands make the "
                "label depend on the order the rules are checked")
        if self.regime_swingy_straightness >= self.regime_biased_straightness:
            raise ValueError(
                "regime_swingy_straightness must be below "
                "regime_biased_straightness for the same reason")
```

- [ ] **Step 5: Write the classifier**

Insert into `btc_5m_predictor.py` immediately after `measure_round_shape`:

```python
def classify_regime(recent: Sequence[RoundShape],
                    baseline: Sequence[RoundShape],
                    cfg: Config) -> tuple[str, dict[str, float]]:
    """
    Which of three states the recent rounds look like, and the numbers why.

    Two axes decide it. How far the market travelled says whether anything
    is happening; how straight the path was says whether what happened went
    somewhere. FLAT is low travel, BIASED is straight travel that ends far
    from the strike, SWINGY is travel that crosses back and ends nowhere.

    Both size axes are expressed as ratios of the recent window's median to
    the baseline's, never as absolute numbers. A market's ordinary travel in
    a quiet hour and a wild one differ by an order of magnitude, and a fixed
    threshold would simply relabel the same behaviour when volatility moved.

    Returns ("", {}) when there is not enough history, and (label, measures)
    otherwise -- where an empty label means no rule matched. That is not a
    fourth state; it is an absence of evidence, and the caller holds what it
    already had.
    """
    if len(recent) < cfg.regime_min_rounds or not baseline:
        return "", {}

    def median_of(rows: Sequence[RoundShape], attr: str) -> float:
        return statistics.median([getattr(r, attr) for r in rows])

    base_travel = median_of(baseline, "travel")
    base_terminal = median_of(baseline, "terminal")
    if base_travel <= 0 or base_terminal <= 0:
        return "", {}

    travel_ratio = median_of(recent, "travel") / base_travel
    terminal_ratio = median_of(recent, "terminal") / base_terminal
    straightness = median_of(recent, "straightness")
    crossings = statistics.fmean([float(r.crossings) for r in recent])

    measures = {"travel_ratio": travel_ratio,
                "terminal_ratio": terminal_ratio,
                "straightness": straightness,
                "crossings": crossings,
                "rounds": float(len(recent))}

    if (travel_ratio < cfg.regime_flat_travel_ratio
            and terminal_ratio < cfg.regime_flat_terminal_ratio):
        return "FLAT", measures
    if (straightness >= cfg.regime_biased_straightness
            and terminal_ratio >= cfg.regime_biased_terminal_ratio
            and crossings <= cfg.regime_biased_max_crossings):
        return "BIASED", measures
    if (crossings >= cfg.regime_swingy_min_crossings
            and straightness < cfg.regime_swingy_straightness
            and travel_ratio >= cfg.regime_swingy_travel_ratio):
        return "SWINGY", measures
    return "", measures
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestRegimeClassification -v`
Expected: PASS, 8 tests

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Name the market's state by how the recent rounds differ from the last hundred"
```

---

### Task 3: Hysteresis and the reading

A state that flips every poll is noise. Commit a candidate only after it holds, and report the gap as "entering".

**Files:**
- Modify: `btc_5m_predictor.py` — insert after `classify_regime`
- Test: `test_btc_5m.py` — append `TestRegimeTracker`

**Interfaces:**
- Consumes: `classify_regime` from Task 2
- Produces: `RegimeReading` frozen dataclass with fields `current: str`, `previous: str | None`, `previous_age_min: float`, `entering: str | None`, `entering_count: int`, `commit_threshold: int`, `measures: tuple[tuple[str, float], ...]`, and method `describe() -> str`. Class `RegimeTracker(threshold: int)` with method `update(label: str, now: float, measures: dict[str, float]) -> RegimeReading` and property `reading -> RegimeReading`.

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestRegimeTracker(unittest.TestCase):

    def test_starts_unknown_and_steady(self):
        tracker = m.RegimeTracker(3)
        reading = tracker.reading
        self.assertEqual(reading.current, "UNKNOWN")
        self.assertIsNone(reading.previous)
        self.assertIsNone(reading.entering)

    def test_candidate_must_hold_before_it_commits(self):
        tracker = m.RegimeTracker(3)
        r1 = tracker.update("FLAT", 0.0, {})
        self.assertEqual(r1.current, "UNKNOWN")
        self.assertEqual(r1.entering, "FLAT")
        self.assertEqual(r1.entering_count, 1)
        r2 = tracker.update("FLAT", 300.0, {})
        self.assertEqual(r2.current, "UNKNOWN")
        self.assertEqual(r2.entering_count, 2)
        r3 = tracker.update("FLAT", 600.0, {})
        self.assertEqual(r3.current, "FLAT")
        self.assertIsNone(r3.entering)

    def test_a_single_odd_round_does_not_flip_the_state(self):
        tracker = m.RegimeTracker(3)
        for t in (0.0, 300.0, 600.0):
            tracker.update("FLAT", t, {})
        tracker.update("SWINGY", 900.0, {})
        reading = tracker.update("FLAT", 1200.0, {})
        self.assertEqual(reading.current, "FLAT")
        self.assertIsNone(reading.entering)

    def test_previous_state_and_its_age_are_reported(self):
        tracker = m.RegimeTracker(2)
        tracker.update("FLAT", 0.0, {})
        tracker.update("FLAT", 300.0, {})          # commits FLAT at t=300
        tracker.update("BIASED", 600.0, {})
        reading = tracker.update("BIASED", 900.0, {})  # commits BIASED
        self.assertEqual(reading.current, "BIASED")
        self.assertEqual(reading.previous, "FLAT")
        self.assertAlmostEqual(reading.previous_age_min, 0.0, places=6)
        later = tracker.update("BIASED", 3300.0, {})
        self.assertAlmostEqual(later.previous_age_min, 40.0, places=6)

    def test_unclassifiable_reading_holds_and_clears_the_candidate(self):
        tracker = m.RegimeTracker(3)
        for t in (0.0, 300.0, 600.0):
            tracker.update("FLAT", t, {})
        tracker.update("SWINGY", 900.0, {})
        reading = tracker.update("", 1200.0, {})
        self.assertEqual(reading.current, "FLAT")
        self.assertIsNone(reading.entering)
        self.assertEqual(reading.entering_count, 0)

    def test_describe_reads_as_the_required_sentence(self):
        tracker = m.RegimeTracker(3)
        for t in (0.0, 300.0, 600.0):
            tracker.update("FLAT", t, {})
        for t in (900.0, 1200.0, 1500.0):
            tracker.update("BIASED", t, {})
        reading = tracker.update("SWINGY", 3900.0, {})
        text = reading.describe()
        self.assertIn("exited FLAT", text)
        self.assertIn("40m ago", text)
        self.assertIn("currently BIASED", text)
        self.assertIn("entering SWINGY (1/3)", text)

    def test_describe_says_steady_when_nothing_is_pending(self):
        tracker = m.RegimeTracker(1)
        reading = tracker.update("FLAT", 0.0, {})
        self.assertIn("currently FLAT", reading.describe())
        self.assertIn("steady", reading.describe())

    def test_threshold_below_one_is_clamped(self):
        tracker = m.RegimeTracker(0)
        reading = tracker.update("FLAT", 0.0, {})
        self.assertEqual(reading.current, "FLAT")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestRegimeTracker -v`
Expected: FAIL with `AttributeError: module 'btc_5m_predictor' has no attribute 'RegimeTracker'`

- [ ] **Step 3: Write the implementation**

Insert into `btc_5m_predictor.py` immediately after `classify_regime`:

```python
@dataclass(frozen=True)
class RegimeReading:
    """
    What state the market is in, what it left, and what it may be entering.

    `entering` is NOT a forecast. It is the candidate state that the recent
    evidence matches but that has not yet held long enough to be committed,
    reported with its progress so the gap is visible rather than hidden
    inside the detector. Read it as "the evidence has moved and has not
    settled", never as "this will happen".
    """

    current: str = "UNKNOWN"
    previous: str | None = None
    previous_age_min: float = 0.0
    entering: str | None = None
    entering_count: int = 0
    commit_threshold: int = 1
    # Tuple of pairs rather than a dict, so the reading stays hashable and
    # frozen like every other dataclass in this file.
    measures: tuple[tuple[str, float], ...] = ()

    def describe(self) -> str:
        parts = []
        if self.previous is not None:
            parts.append(f"exited {self.previous} "
                         f"{self.previous_age_min:.0f}m ago")
        parts.append(f"currently {self.current}")
        if self.entering is not None:
            parts.append(f"entering {self.entering} "
                         f"({self.entering_count}/{self.commit_threshold})")
        else:
            parts.append("steady")
        return "MARKET " + " | ".join(parts)

    def detail(self) -> str:
        if not self.measures:
            return "no measurements yet"
        return "  ".join(f"{k}={v:.2f}" for k, v in self.measures)


class RegimeTracker:
    """
    Commits a state only once it has held, and reports the gap.

    Without this the label would change on any single unusual round, and the
    bot would swap strategy mid-hour on noise -- which is worse than never
    switching at all, because it pays the cost of every transition and
    collects the benefit of none.
    """

    def __init__(self, threshold: int) -> None:
        self._threshold = max(1, threshold)
        self._current = "UNKNOWN"
        self._previous: str | None = None
        self._previous_at: float | None = None
        self._candidate: str | None = None
        self._count = 0
        self._reading = RegimeReading(commit_threshold=self._threshold)

    @property
    def reading(self) -> RegimeReading:
        return self._reading

    def update(self, label: str, now: float,
               measures: dict[str, float]) -> RegimeReading:
        if not label or label == self._current:
            # Either nothing matched, or the market is still where it was.
            # Both clear the candidate: progress toward a change is only
            # meaningful while the evidence keeps pointing the same way.
            self._candidate = None
            self._count = 0
        else:
            if label == self._candidate:
                self._count += 1
            else:
                self._candidate = label
                self._count = 1
            if self._count >= self._threshold:
                self._previous = self._current
                self._previous_at = now
                self._current = label
                self._candidate = None
                self._count = 0

        age_min = (0.0 if self._previous_at is None
                   else max(0.0, (now - self._previous_at) / 60.0))
        self._reading = RegimeReading(
            current=self._current,
            previous=self._previous,
            previous_age_min=age_min,
            entering=self._candidate,
            entering_count=self._count,
            commit_threshold=self._threshold,
            measures=tuple(sorted(measures.items())))
        return self._reading
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestRegimeTracker -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Refuse to call a single odd round a change of market state"
```

---

### Task 4: Feed the detector from the closes already fetched

**Files:**
- Modify: `btc_5m_predictor.py` — `VolatilityEstimator.__init__` (line 1864), `sigma_annual` (line 1878), plus two new methods
- Test: `test_btc_5m.py` — append `TestRegimeFromKlines`

**Interfaces:**
- Consumes: `measure_round_shape`, `classify_regime`, `RegimeTracker`, `RegimeReading`
- Produces: `VolatilityEstimator.regime(symbol: str | None = None) -> RegimeReading`, and `VolatilityEstimator._measure_regime(closes: list[float], symbol: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestRegimeFromKlines(unittest.TestCase):
    """The detector reads the closes sigma_annual already fetched."""

    def _estimator(self, closes):
        cfg = m.Config(api_key="k", api_secret="s")

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                # Binance kline rows: index 4 is the close.
                return [[0, "0", "0", "0", f"{c}", "0"] for c in closes]

        class FakeSession:
            def get(self, url, params=None, timeout=None):
                return FakeResp()

        return m.VolatilityEstimator(cfg, FakeSession())

    def test_regime_is_unknown_before_sigma_is_called(self):
        est = self._estimator([100.0] * 600)
        self.assertEqual(est.regime("BTCUSDT").current, "UNKNOWN")

    def test_a_steadily_climbing_market_reads_biased(self):
        # 600 minutes climbing without a pullback: every 5m block is a
        # straight run that ends far above where it opened.
        closes = [100.0 * (1.0006 ** i) for i in range(600)]
        est = self._estimator(closes)
        for _ in range(5):
            est._cache.clear()
            est.sigma_annual("BTCUSDT")
        self.assertEqual(est.regime("BTCUSDT").current, "BIASED")

    def test_regime_survives_a_short_close_series(self):
        est = self._estimator([100.0 + (i % 3) for i in range(40)])
        est.sigma_annual("BTCUSDT")
        self.assertIn(est.regime("BTCUSDT").current,
                      ("UNKNOWN", "FLAT", "BIASED", "SWINGY"))

    def test_reading_carries_its_measurements(self):
        closes = [100.0 * (1.0006 ** i) for i in range(600)]
        est = self._estimator(closes)
        est.sigma_annual("BTCUSDT")
        reading = est.regime("BTCUSDT")
        keys = dict(reading.measures)
        self.assertIn("travel_ratio", keys)
        self.assertIn("crossings", keys)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestRegimeFromKlines -v`
Expected: FAIL with `AttributeError: 'VolatilityEstimator' object has no attribute 'regime'`

- [ ] **Step 3: Add the tracker store to `__init__`**

In `VolatilityEstimator.__init__`, immediately after `self._trend: dict[str, Trend] = {}`, add:

```python
        self._trackers: dict[str, RegimeTracker] = {}
        self._regime: dict[str, RegimeReading] = {}
```

- [ ] **Step 4: Call the detector from `sigma_annual`**

In `sigma_annual`, immediately after the existing line `self._trend[symbol] = self._measure_trend(closes)`, add:

```python
        # Same closes as the sigma and the trend, deliberately. A regime read
        # off a different fetch than the volatility it is scaled by is two
        # snapshots of two moments pretending to be one.
        self._measure_regime(closes, symbol)
```

- [ ] **Step 5: Write the two methods**

Insert into `VolatilityEstimator` immediately after `_measure_trend`:

```python
    def _measure_regime(self, closes: list[float], symbol: str) -> None:
        """
        Cut the closes into rounds, classify the recent ones, and commit.

        Costs one pass over data already in memory and no network at all.
        """
        cfg = self._cfg
        window = [c for c in closes if c > 0]
        block = max(1, round(cfg.round_seconds / 60.0))
        tracker = self._trackers.setdefault(
            symbol, RegimeTracker(cfg.regime_commit_readings))
        if len(window) < block * 2 + 1:
            self._regime[symbol] = tracker.reading
            return

        steps = [math.log(b / a) for a, b in itertools.pairwise(window)]
        sd_step = statistics.pstdev(steps)
        if sd_step <= 0:
            self._regime[symbol] = tracker.reading
            return
        sigma_block = sd_step * math.sqrt(block)

        # Blocks of one round each, oldest first, each including the close
        # it opened at as its first sample -- that open is the strike.
        edges = list(range(len(window) - 1, -1, -block))[::-1]
        shapes = [measure_round_shape(window[a:b + 1], sigma_block)
                  for a, b in itertools.pairwise(edges)]
        if len(shapes) < cfg.regime_min_rounds + 1:
            self._regime[symbol] = tracker.reading
            return

        recent = shapes[-cfg.regime_recent_rounds:]
        baseline = shapes[:-cfg.regime_recent_rounds] or shapes
        label, measures = classify_regime(recent, baseline, cfg)
        self._regime[symbol] = tracker.update(label, time.time(), measures)

    def regime(self, symbol: str | None = None) -> RegimeReading:
        """Regime state for `symbol`. Call sigma_annual first."""
        return self._regime.get(symbol or self._cfg.symbol, RegimeReading())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestRegimeFromKlines -v`
Expected: PASS, 4 tests

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Read the market's state from the closes the volatility already cost us"
```

---

### Task 5: Config fields for `pin` and `adaptive`

**Files:**
- Modify: `btc_5m_predictor.py` — `Config` fields near line 341, validation in `__post_init__`
- Test: `test_btc_5m.py` — append `TestPinConfigValidation`

**Interfaces:**
- Consumes: nothing
- Produces: `Config.adaptive: bool`, `Config.pin: bool`, `Config.pin_window_s: float`, `Config.pin_min_sigmas: float`, `Config.pin_max_round_travel: float`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPinConfigValidation(unittest.TestCase):

    def _cfg(self, **kw):
        return m.Config(api_key="k", api_secret="s", **kw)

    def test_defaults_are_off(self):
        cfg = self._cfg()
        self.assertFalse(cfg.pin)
        self.assertFalse(cfg.adaptive)

    def test_pin_and_straddle_together_are_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._cfg(pin=True, straddle=True)
        self.assertIn("at most one", str(ctx.exception))

    def test_non_positive_window_is_refused(self):
        with self.assertRaises(ValueError):
            self._cfg(pin=True, pin_window_s=0.0)

    def test_negative_min_sigmas_is_refused(self):
        with self.assertRaises(ValueError):
            self._cfg(pin=True, pin_min_sigmas=-0.1)

    def test_non_positive_round_travel_is_refused(self):
        with self.assertRaises(ValueError):
            self._cfg(pin=True, pin_max_round_travel=0.0)

    def test_window_longer_than_the_round_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._cfg(pin=True, pin_window_s=600.0, round_seconds=300)
        self.assertIn("round_seconds", str(ctx.exception))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPinConfigValidation -v`
Expected: FAIL with `TypeError: Config.__init__() got an unexpected keyword argument 'pin'`

- [ ] **Step 3: Add the fields**

In `Config`, immediately after the regime block added in Task 2, insert:

```python
    # --- Orchestration ----------------------------------------------------
    # True only on the "adaptive" profile. Turns on regime-driven strategy
    # selection; every other profile still pins one strategy exactly as
    # before, which is what keeps an A/B comparison possible.
    adaptive: bool = False

    # --- pin: the FLAT strategy -------------------------------------------
    # Late in a round, in a market that has not moved for an hour, back the
    # side spot is already on.
    #
    # This is the first strategy in this file that trades a conviction the
    # pricing model cannot express. In FLAT, spot sits near the strike, so
    # digital_up_probability returns about 0.50 while the venue quotes 0.65
    # on the side spot is on -- the model therefore sees NEGATIVE edge and
    # every model-driven profile correctly refuses. The claim pin makes is
    # not that the model is wrong about distance; it is that the regime has
    # measured something the model does not look at, namely whether that
    # distance moves at all.
    #
    # It is also the profile that pays if FLAT detection is wrong. At the
    # 0.85 ceiling a win pays about 17% and a loss costs the stake, so one
    # break erases roughly six wins. pin_max_round_travel is what earns that
    # asymmetry: the regime describes 24 rounds, and THIS round may be the
    # one that breaks out.
    pin: bool = False
    pin_window_s: float = 75.0
    pin_min_sigmas: float = 0.15
    pin_max_round_travel: float = 1.0
```

- [ ] **Step 4: Add the validation**

In `Config.__post_init__`, immediately after the regime validation added in Task 2, insert:

```python
        if self.pin and self.straddle:
            raise ValueError(
                "pin and straddle are different entry paths; enable at most "
                "one of them on a profile")
        if self.pin_window_s <= 0:
            raise ValueError("pin_window_s must be positive")
        if self.pin_window_s > self.round_seconds:
            raise ValueError(
                f"pin_window_s {self.pin_window_s} exceeds round_seconds "
                f"{self.round_seconds}; the window would cover the whole "
                f"round and the strategy's premise is that little time is "
                f"left")
        if self.pin_min_sigmas < 0:
            raise ValueError("pin_min_sigmas must be non-negative")
        if self.pin_max_round_travel <= 0:
            raise ValueError("pin_max_round_travel must be positive")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPinConfigValidation -v`
Expected: PASS, 6 tests

- [ ] **Step 6: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Give pin its own settings, and refuse to enable two entry paths at once"
```

---

### Task 6: Profiles, playbooks, and the pinned envelope

**Files:**
- Modify: `btc_5m_predictor.py` — `PROFILES` dict (ends line 979), then new module constants and a validator immediately after it
- Test: `test_btc_5m.py` — append `TestPlaybooks`

**Interfaces:**
- Consumes: `PROFILES`, `Config`
- Produces: `ADAPTIVE_ENVELOPE: dict[str, float | int]`, `PLAYBOOKS: dict[str, tuple[tuple[str, dict], ...]]`, `validate_playbooks() -> None`, and two new entries in `PROFILES`: `"pin"` and `"adaptive"`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPlaybooks(unittest.TestCase):

    def test_every_playbook_entry_names_a_real_profile(self):
        for regime, entries in m.PLAYBOOKS.items():
            self.assertTrue(entries, f"{regime} has no entries")
            for name, _ in entries:
                self.assertIn(name, m.PROFILES)

    def test_the_three_regimes_all_have_a_playbook(self):
        self.assertEqual(set(m.PLAYBOOKS), {"FLAT", "BIASED", "SWINGY"})

    def test_no_overlay_touches_a_governance_field(self):
        for _, entries in m.PLAYBOOKS.items():
            for _, overlay in entries:
                self.assertFalse(set(overlay) & set(m.ADAPTIVE_ENVELOPE))

    def test_validator_rejects_an_unknown_profile(self):
        original = dict(m.PLAYBOOKS)
        try:
            m.PLAYBOOKS["FLAT"] = (("no_such_profile", {}),)
            with self.assertRaises(ValueError) as ctx:
                m.validate_playbooks()
            self.assertIn("no_such_profile", str(ctx.exception))
        finally:
            m.PLAYBOOKS.clear()
            m.PLAYBOOKS.update(original)

    def test_validator_rejects_a_governance_override(self):
        original = dict(m.PLAYBOOKS)
        try:
            m.PLAYBOOKS["FLAT"] = (("straddle",
                                    {"daily_loss_limit_pct": 0.9}),)
            with self.assertRaises(ValueError) as ctx:
                m.validate_playbooks()
            self.assertIn("daily_loss_limit_pct", str(ctx.exception))
        finally:
            m.PLAYBOOKS.clear()
            m.PLAYBOOKS.update(original)

    def test_validator_rejects_an_unknown_config_key_in_an_overlay(self):
        original = dict(m.PLAYBOOKS)
        try:
            m.PLAYBOOKS["FLAT"] = (("straddle", {"nonsense_knob": 1}),)
            with self.assertRaises(ValueError) as ctx:
                m.validate_playbooks()
            self.assertIn("nonsense_knob", str(ctx.exception))
        finally:
            m.PLAYBOOKS.clear()
            m.PLAYBOOKS.update(original)

    def test_the_shipped_playbooks_validate(self):
        m.validate_playbooks()

    def test_pin_profile_builds_a_valid_config(self):
        doc = m.default_config_document("pin")
        cfg = m.build_config(doc, api_key="k", api_secret="s", live=False,
                             db_path=":memory:", profile="pin")
        self.assertTrue(cfg.pin)
        self.assertFalse(cfg.straddle)
        self.assertLessEqual(cfg.max_blended_price, cfg.max_entry_price)
        self.assertGreaterEqual(cfg.max_blended_price, cfg.min_entry_price)

    def test_adaptive_profile_builds_and_carries_the_envelope(self):
        doc = m.default_config_document("adaptive")
        cfg = m.build_config(doc, api_key="k", api_secret="s", live=False,
                             db_path=":memory:", profile="adaptive")
        self.assertTrue(cfg.adaptive)
        for key, value in m.ADAPTIVE_ENVELOPE.items():
            self.assertEqual(getattr(cfg, key), value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPlaybooks -v`
Expected: FAIL with `AttributeError: module 'btc_5m_predictor' has no attribute 'PLAYBOOKS'`

- [ ] **Step 3: Add the two profiles**

Inside the `PROFILES` dict in `btc_5m_predictor.py`, immediately before the closing `}` of the dict, add:

```python
    # THE FLAT STRATEGY. Back the side spot already sits on, late in a round,
    # in a market that has spent an hour going nowhere. See the pin_* fields
    # on Config for why this is not simply a looser "favorite".
    "pin": {"pin": True, "pin_window_s": 75.0, "pin_min_sigmas": 0.15,
            "pin_max_round_travel": 1.0,
            "min_entry_price": 0.50, "max_entry_price": 0.85,
            # A win must pay at least 15% of the stake. This is what stops
            # the profile drifting into the 0.90+ band where a single break
            # erases ten wins -- the trap buffer's own notes were written
            # about, and the reason that profile carries a 0.25 floor.
            "min_win_return": 0.15,
            "max_stake_pct": 0.10, "min_stake_usdt": 1.0,
            "daily_loss_limit_pct": 0.35, "assumed_spread_pct": 0.04,
            "kelly_fraction": 0.25, "min_liquidity": 0.0,
            "max_rounds_per_day": 250, "max_consecutive_losses": 8,
            "paper_start_bankroll": 25.0,
            # Both of the next two are INERT -- the pin path never calls
            # clears_edge and never scales in -- and both are stated anyway
            # because Config validates them. min_edge must be strictly
            # inside (0, 1), so a 0.0 would raise at startup; and
            # max_blended_price must lie within this profile's own entry
            # band, so inheriting one sized for a different band would also
            # raise. Straddle declares a min_edge its path never reads for
            # exactly the same reason.
            "min_edge": 0.02, "min_edge_ratio": 0.0,
            "scale_in": False, "max_blended_price": 0.80},
    # The orchestrator. Selecting this profile is what turns regime-driven
    # strategy selection on; every other profile still pins one strategy for
    # the whole session exactly as it did before.
    #
    # Its own entry parameters are never used: under `adaptive` the entry
    # config is resolved per round from PLAYBOOKS. What this profile carries
    # that matters is the ENVELOPE -- the day-accounted limits, spread here
    # so that RiskManager, which reads the running config live, sees one
    # fixed set of them no matter which strategy is trading.
    "adaptive": {"adaptive": True, "assumed_spread_pct": 0.05,
                 "kelly_fraction": 0.25, "min_liquidity": 0.0,
                 "paper_start_bankroll": 100.0,
                 "max_concurrent_positions": 4,
                 **ADAPTIVE_ENVELOPE},
```

- [ ] **Step 4: Add the envelope, playbooks and validator**

Insert into `btc_5m_predictor.py` immediately BEFORE the `PROFILES` dict (the envelope must exist before `PROFILES` uses it):

```python
# --------------------------------------------------------------------------
# The pinned risk envelope
# --------------------------------------------------------------------------

# WHY THESE THREE ARE FIXED FOR A SESSION
# ---------------------------------------
# RiskManager reads the running config live on every check, so if a regime
# switch swapped the whole profile these limits would move underneath a day
# that has already been partly spent. Switching straddle (0.50) to buffer
# (0.35) after a 40% drawdown halts the bot instantly on a limit it was never
# trading under; switching the other way silently lifts a halt that had
# already tripped. A limit that moves mid-day is not a limit.
#
# The values are DECLARED, not derived. Taking the minimum across reachable
# profiles would hand a straddle day buffer's 0.35, which halts after 1.75
# bad rounds -- strictly worse than running straddle alone, arrived at by a
# rule nobody wrote down.
#
# 0.50 is chosen: the worst single round across reachable regimes is
# straddle's roughly 20% of bankroll, so 0.50 buys 2.5 bad rounds, the same
# headroom every other profile is given.
#
# max_consecutive_losses is the weakest number here and is recorded as such.
# A straddle "loss" is a bad round and a buffer loss is a full stake, so the
# counter measures two different things under one name. Revisit once a paper
# run has produced streak data.
ADAPTIVE_ENVELOPE: dict[str, float | int] = {
    "daily_loss_limit_pct": 0.50,
    "max_consecutive_losses": 10,
    "max_rounds_per_day": 400,
}
```

Then insert immediately AFTER the `PROFILES` dict:

```python
# --------------------------------------------------------------------------
# Playbooks: which strategy suits which market
# --------------------------------------------------------------------------

# An ordered list per regime. Order is priority: each entry declines on its
# own terms -- price band, window, gates -- and the first that opens a
# position wins the round.
#
# The overlay is what makes this more than profile selection. BIASED does not
# merely fall back to straddle; it falls back to straddle with its first-leg
# ceiling loosened, because in a market that has picked a side the cheap side
# is cheaper and 0.25 would almost never be met.
#
# FLAT needs no new mechanism for its time split. straddle already self-gates
# to the first 240s of the round via straddle_entry_window_s, and pin gates
# itself to the last 75s. They overlap between 225s and 240s elapsed, and
# straddle wins that overlap by being listed first -- deliberately, because
# inside the overlap a genuinely cheap leg is the better trade and pin should
# only pick the round up once straddle's own window has closed.
PLAYBOOKS: dict[str, tuple[tuple[str, dict], ...]] = {
    # Spot crosses the strike repeatedly. Straddle's premise holds, and this
    # is the only state it was ever true in.
    "SWINGY": (("straddle", {}),),
    # Spot picks a side and stays there. Buffer is the strategy; straddle
    # remains as a fallback for the flip this state does produce once or
    # twice, with its first-leg ceiling loosened to match a market where the
    # unfavoured side is genuinely cheap.
    "BIASED": (("buffer", {}),
               ("straddle", {"straddle_first_leg_max_price": 0.30})),
    # Barely moves, finishes on whichever side it was already on. Straddle
    # early while a cheap side can still appear, pin late once it cannot.
    "FLAT": (("straddle", {}),
             ("pin", {})),
}


def validate_playbooks() -> None:
    """
    Every entry names a real profile, sets real knobs, and leaves the
    envelope alone.

    Checked at startup rather than trusted, because all three failures are
    silent at runtime: an unknown profile name would raise deep inside an
    entry attempt, an unknown key would be dropped and the overlay would
    quietly do nothing, and a governance override would defeat the entire
    reason the envelope exists.
    """
    fields = {f.name for f in dataclasses.fields(Config)}
    for regime, entries in PLAYBOOKS.items():
        if not entries:
            raise ValueError(f"playbook {regime!r} has no entries")
        for name, overlay in entries:
            if name not in PROFILES:
                raise ValueError(
                    f"playbook {regime!r} names unknown profile {name!r}")
            unknown = sorted(set(overlay) - fields)
            if unknown:
                raise ValueError(
                    f"playbook {regime!r} entry {name!r} sets unknown "
                    f"config field(s) {unknown}")
            clash = sorted(set(overlay) & set(ADAPTIVE_ENVELOPE))
            if clash:
                raise ValueError(
                    f"playbook {regime!r} entry {name!r} overrides "
                    f"governance field(s) {clash}; those are pinned for the "
                    f"session by ADAPTIVE_ENVELOPE because they are "
                    f"accounted against a day, not a round")
```

Confirm `import dataclasses` is present at the top of the file. The module already uses `from dataclasses import dataclass` and `replace`; add `import dataclasses` if the bare module import is missing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPlaybooks -v`
Expected: PASS, 9 tests

- [ ] **Step 6: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Write down which strategy suits which market, and pin the day's limits"
```

---

### Task 7: Resolve a playbook entry into an effective config

**Files:**
- Modify: `btc_5m_predictor.py` — new module function after `validate_playbooks`
- Test: `test_btc_5m.py` — append `TestPlaybookResolution`

**Interfaces:**
- Consumes: `PROFILES`, `PLAYBOOKS`, `ADAPTIVE_ENVELOPE`, `_coerce`, `Config`
- Produces: `resolve_playbook_entry(base: Config, profile: str, overlay: dict) -> Config`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPlaybookResolution(unittest.TestCase):

    def _base(self):
        doc = m.default_config_document("adaptive")
        return m.build_config(doc, api_key="k", api_secret="s", live=False,
                              db_path=":memory:", profile="adaptive")

    def test_entry_parameters_come_from_the_named_profile(self):
        cfg = m.resolve_playbook_entry(self._base(), "buffer", {})
        self.assertEqual(cfg.min_buffer_sigmas,
                         m.PROFILES["buffer"]["min_buffer_sigmas"])
        self.assertEqual(cfg.max_entry_price,
                         m.PROFILES["buffer"]["max_entry_price"])

    def test_overlay_wins_over_the_profile(self):
        cfg = m.resolve_playbook_entry(
            self._base(), "straddle", {"straddle_first_leg_max_price": 0.30})
        self.assertEqual(cfg.straddle_first_leg_max_price, 0.30)

    def test_governance_is_pinned_across_every_reachable_entry(self):
        base = self._base()
        for _, entries in m.PLAYBOOKS.items():
            for name, overlay in entries:
                cfg = m.resolve_playbook_entry(base, name, overlay)
                for key, value in m.ADAPTIVE_ENVELOPE.items():
                    self.assertEqual(
                        getattr(cfg, key), value,
                        f"{name} moved the pinned field {key}")

    def test_buffers_own_daily_limit_is_overridden_by_the_envelope(self):
        # buffer declares 0.35; under adaptive the envelope's 0.50 wins.
        self.assertEqual(m.PROFILES["buffer"]["daily_loss_limit_pct"], 0.35)
        cfg = m.resolve_playbook_entry(self._base(), "buffer", {})
        self.assertEqual(cfg.daily_loss_limit_pct, 0.50)

    def test_identity_and_credentials_come_from_the_base(self):
        base = self._base()
        cfg = m.resolve_playbook_entry(base, "buffer", {})
        self.assertEqual(cfg.api_key, base.api_key)
        self.assertEqual(cfg.api_secret, base.api_secret)
        self.assertEqual(cfg.db_path, base.db_path)
        self.assertEqual(cfg.live, base.live)

    def test_profile_name_records_which_entry_produced_the_config(self):
        cfg = m.resolve_playbook_entry(self._base(), "pin", {})
        self.assertEqual(cfg.profile_name, "pin")

    def test_the_resolved_config_is_never_adaptive(self):
        # An entry config that still claimed to be adaptive would send the
        # dispatcher back through the playbook and recurse.
        for _, entries in m.PLAYBOOKS.items():
            for name, overlay in entries:
                cfg = m.resolve_playbook_entry(self._base(), name, overlay)
                self.assertFalse(cfg.adaptive)

    def test_every_reachable_entry_resolves_without_raising(self):
        base = self._base()
        for _, entries in m.PLAYBOOKS.items():
            for name, overlay in entries:
                m.resolve_playbook_entry(base, name, overlay)

    def test_unknown_profile_raises(self):
        with self.assertRaises(KeyError):
            m.resolve_playbook_entry(self._base(), "no_such", {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPlaybookResolution -v`
Expected: FAIL with `AttributeError: module 'btc_5m_predictor' has no attribute 'resolve_playbook_entry'`

- [ ] **Step 3: Write the implementation**

Insert into `btc_5m_predictor.py` immediately after `validate_playbooks`:

```python
def resolve_playbook_entry(base: Config, profile: str,
                           overlay: dict) -> Config:
    """
    The config one playbook entry trades under.

    Entry parameters come from the named profile, then the overlay on top.
    The envelope goes on last and therefore wins, which is the whole point:
    a profile's own daily_loss_limit_pct is correct when that profile runs
    the session alone and wrong when it is one of three sharing a day.

    Identity, credentials, journal path and live/paper are NOT taken from
    the profile. They come from `base` because they belong to the process,
    not to the strategy, and because a resolved config that changed them
    would sign with different credentials or split one session's journal
    across two files.

    `adaptive` is forced off. An entry config that still claimed to be
    adaptive would send the dispatcher back through the playbook and
    recurse.
    """
    if profile not in PROFILES:
        raise KeyError(f"unknown profile {profile!r}")

    fields = {f.name for f in dataclasses.fields(Config)}
    values: dict[str, object] = {}
    for source in (PROFILES[profile], overlay):
        for key, value in source.items():
            if key in fields:
                values[key] = _coerce(key, value)
    values.update(ADAPTIVE_ENVELOPE)

    # Process-level identity stays with the process.
    for name in IMMUTABLE_FIELDS:
        values.pop(name, None)
    values.pop("live", None)

    values["adaptive"] = False
    # Recorded so a Position and its journal row can say which entry opened
    # them, which is what makes --calibration-report able to answer which
    # regime's playbook actually made money.
    return replace(base, profile_name=profile, **values)
```

Note: `profile_name` is in `IMMUTABLE_FIELDS`, so it is popped from `values` by the loop above and then set explicitly as a keyword. That is deliberate — `IMMUTABLE_FIELDS` governs what a *disk reload* may change, not what this function may set.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPlaybookResolution -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Let a playbook entry pick its strategy but never the day's limits"
```

---

### Task 8: A position remembers what opened it

Without this, a `buffer` position is managed under whatever strategy the regime later selected.

**Files:**
- Modify: `btc_5m_predictor.py` — `Position` (line 1403), `Journal.record` (line 3271), the two position-open sites (lines 4791 and 5195)
- Test: `test_btc_5m.py` — append `TestPositionProvenance`

**Interfaces:**
- Consumes: `Config`
- Produces: `Position.profile: str` (default `"unknown"`), `Position.entry_cfg: Config | None` (default `None`); `Journal.record(..., profile: str | None = None)`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPositionProvenance(unittest.TestCase):

    def _round(self):
        return m.Round(
            topic_id=1, market_id=1, vendor="v", slug="s", symbol="BTCUSDT",
            start_ms=0, end_ms=300_000, up_token_id="u", down_token_id="d",
            up_quote=0.5, down_quote=0.5, fee_bps=200, chain_id="1",
            collateral="USDT", venue_slippage_bps=0, decimal_precision=2,
            liquidity=1000.0, strike=100.0)

    def _signal(self):
        return m.Signal(side=m.Side.UP, model_prob=0.6, fill_price=0.65,
                        edge=0.05, stake_usdt=1.0, seconds_left=100.0)

    def test_position_defaults_keep_existing_construction_working(self):
        pos = m.Position(1, self._round(), self._signal(), 1.0, 1)
        self.assertEqual(pos.profile, "unknown")
        self.assertIsNone(pos.entry_cfg)

    def test_position_carries_the_profile_that_opened_it(self):
        cfg = m.Config(api_key="k", api_secret="s")
        pos = m.Position(1, self._round(), self._signal(), 1.0, 1,
                         profile="buffer", entry_cfg=cfg)
        self.assertEqual(pos.profile, "buffer")
        self.assertIs(pos.entry_cfg, cfg)

    def test_replace_preserves_provenance(self):
        cfg = m.Config(api_key="k", api_secret="s")
        pos = m.Position(1, self._round(), self._signal(), 1.0, 1,
                         profile="buffer", entry_cfg=cfg)
        grown = replace(pos, committed_usdt=2.0, tranches=2)
        self.assertEqual(grown.profile, "buffer")
        self.assertIs(grown.entry_cfg, cfg)

    def test_journal_records_the_per_trade_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "j.db")
            journal = m.Journal(path, profile="adaptive")
            tid = journal.record("PAPER", self._round(), self._signal(),
                                 100.0, 0.5, 50.0, None, profile="pin")
            row = journal._conn.execute(
                "SELECT profile FROM trades WHERE id=?", (tid,)).fetchone()
            self.assertEqual(row[0], "pin")

    def test_journal_falls_back_to_its_own_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "j.db")
            journal = m.Journal(path, profile="buffer")
            tid = journal.record("PAPER", self._round(), self._signal(),
                                 100.0, 0.5, 50.0)
            row = journal._conn.execute(
                "SELECT profile FROM trades WHERE id=?", (tid,)).fetchone()
            self.assertEqual(row[0], "buffer")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPositionProvenance -v`
Expected: FAIL with `TypeError: Position.__init__() got an unexpected keyword argument 'profile'`

- [ ] **Step 3: Extend `Position`**

In `btc_5m_predictor.py`, change the `Position` dataclass fields from:

```python
    committed_usdt: float = 0.0      # total staked on this round so far
    tranches: int = 1
```

to:

```python
    committed_usdt: float = 0.0      # total staked on this round so far
    tranches: int = 1
    # WHICH STRATEGY OPENED THIS, frozen for the position's life.
    #
    # Under the adaptive profile the regime can change while a position is
    # open, and the strategy selected for the NEXT round has no business
    # managing this one. A buffer position must be topped up under buffer's
    # rules, and a straddle leg must never be topped up at all, regardless of
    # what the market has since become.
    profile: str = "unknown"
    entry_cfg: Config | None = None
```

- [ ] **Step 4: Extend `Journal.record`**

Change the signature from:

```python
    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None) -> int:
```

to:

```python
    def record(self, mode: str, rnd: Round, sig: Signal, spot: float,
               sigma: float, bankroll: float,
               order_id: str | None = None,
               profile: str | None = None) -> int:
        """
        `profile` names the strategy that opened THIS trade.

        Under the adaptive profile a session runs several strategies, so a
        single constructor-time profile would label every row with the
        orchestrator's name and make --calibration-report unable to answer
        the only question worth asking of it: which regime's playbook made
        money. Defaults to the journal's own profile so every existing
        caller keeps its behaviour.
        """
```

and change the value bound into the INSERT from `self._profile` to:

```python
             bankroll, order_id, profile or self._profile, sig.buffer_z,
```

- [ ] **Step 5: Pass provenance at the two open sites**

At line ~4791 (straddle leg open), change:

```python
            self._positions[(raw.symbol, side)] = Position(
```

to include the provenance arguments used by that call site. The straddle path builds its position with the config it entered under; pass `profile=cfg.profile_name, entry_cfg=cfg` where `cfg` is the config that path is using.

At line ~5195 (model path open), change:

```python
            self._positions[(rnd.symbol, sig.side)] = Position(
                tid, rnd, sig, sig.stake_usdt, 1)
```

to:

```python
            self._positions[(rnd.symbol, sig.side)] = Position(
                tid, rnd, sig, sig.stake_usdt, 1,
                profile=cfg.profile_name, entry_cfg=cfg)
```

and pass `profile=cfg.profile_name` to the `self._journal.record(...)` call directly above it. In both cases `cfg` is the config the entry path is operating under; if the surrounding method reads `self._cfg` directly, bind `cfg = self._cfg` at the top of the method first so the same object is used for both the decision and the record.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPositionProvenance -v`
Expected: PASS, 5 tests

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Make a position remember which strategy opened it"
```

---

### Task 9: The `pin` entry path

**Files:**
- Modify: `btc_5m_predictor.py` — new `Trader._maybe_enter_pin` method, placed immediately after `_maybe_enter_model`
- Test: `test_btc_5m.py` — append `TestPinGates`

**Interfaces:**
- Consumes: `Config.pin*`, `RegimeReading`, `Position`, `Journal.record`, `measure_round_shape`
- Produces: `Trader._pin_gate(cfg, reading, seconds_left, excursion_sigmas, round_travel) -> str | None` returning the blocking reason or `None` when clear; `Trader._maybe_enter_pin(bankroll: float, mode: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPinGates(unittest.TestCase):
    """The four gates, tested as pure logic before any I/O is involved."""

    def setUp(self):
        doc = m.default_config_document("pin")
        self.cfg = m.build_config(doc, api_key="k", api_secret="s",
                                  live=False, db_path=":memory:",
                                  profile="pin")
        self.flat = m.RegimeReading(current="FLAT", commit_threshold=3)

    def _gate(self, **kw):
        args = {"cfg": self.cfg, "reading": self.flat, "seconds_left": 60.0,
                "excursion_sigmas": 0.4, "round_travel": 0.5}
        args.update(kw)
        return m.Trader._pin_gate(**args)

    def test_all_gates_clear(self):
        self.assertIsNone(self._gate())

    def test_refuses_outside_flat(self):
        for state in ("UNKNOWN", "BIASED", "SWINGY"):
            reason = self._gate(reading=m.RegimeReading(current=state))
            self.assertEqual(reason, "market is not flat")

    def test_refuses_while_a_transition_is_pending(self):
        pending = m.RegimeReading(current="FLAT", entering="SWINGY",
                                  entering_count=1, commit_threshold=3)
        self.assertEqual(self._gate(reading=pending),
                         "market state is changing")

    def test_refuses_too_early_in_the_round(self):
        self.assertEqual(self._gate(seconds_left=200.0),
                         "too early for a pin")

    def test_refuses_when_spot_sits_on_the_strike(self):
        self.assertEqual(self._gate(excursion_sigmas=0.05),
                         "spot too close to the strike to have a side")

    def test_refuses_a_round_that_is_already_breaking_out(self):
        # The regime describes 24 rounds; this round is not one of them.
        self.assertEqual(self._gate(round_travel=3.0),
                         "this round is already moving")

    def test_gate_order_reports_the_regime_first(self):
        reason = m.Trader._pin_gate(
            cfg=self.cfg, reading=m.RegimeReading(current="SWINGY"),
            seconds_left=200.0, excursion_sigmas=0.01, round_travel=9.0)
        self.assertEqual(reason, "market is not flat")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPinGates -v`
Expected: FAIL with `AttributeError: type object 'Trader' has no attribute '_pin_gate'`

- [ ] **Step 3: Write the gate as a static method**

Insert into `class Trader`, immediately after `_maybe_enter_model`:

```python
    @staticmethod
    def _pin_gate(cfg: Config, reading: RegimeReading, seconds_left: float,
                  excursion_sigmas: float, round_travel: float) -> str | None:
        """
        Why this round cannot be pinned, or None if it can.

        Returns a reason string rather than a bool so _tally_missed can say
        which gate did the refusing. With four gates and no model edge to
        fall back on, "pin did not fire" is otherwise indistinguishable from
        "pin is misconfigured and will never fire".

        Order is deliberate: the regime is reported first because it is the
        precondition the other three only matter inside.
        """
        if reading.current != "FLAT":
            return "market is not flat"
        # A transition in progress is exactly when a pin is most likely to
        # be wrong: the evidence has already moved and simply has not been
        # committed yet.
        if reading.entering is not None:
            return "market state is changing"
        if seconds_left > cfg.pin_window_s:
            return "too early for a pin"
        if excursion_sigmas < cfg.pin_min_sigmas:
            return "spot too close to the strike to have a side"
        # THE GUARD THAT EARNS THE ASYMMETRY. The regime describes the last
        # 24 rounds. This round may be the breakout that ends the state, and
        # a pin bought into a breakout pays 0.15 to lose 0.85.
        if round_travel > cfg.pin_max_round_travel:
            return "this round is already moving"
        return None
```

- [ ] **Step 4: Run the gate tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPinGates -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Write the entry path**

Insert into `class Trader`, immediately after `_pin_gate`:

```python
    def _maybe_enter_pin(self, bankroll: float, mode: str,
                         cfg: Config | None = None) -> None:
        """
        Back the side spot already sits on, late in a flat round.

        The model is deliberately not consulted for direction. In FLAT spot
        is near the strike, so digital_up_probability returns about 0.50 and
        every model-driven gate would refuse -- correctly, given what the
        model looks at. The claim here is about whether the distance MOVES,
        which the model does not measure and the regime does.

        Everything downstream of direction is unchanged: the price band, the
        return floor, liquidity, spread and the risk limits all apply
        exactly as they do on any other path.
        """
        cfg = cfg or self._cfg
        if len(self._positions) >= cfg.max_concurrent_positions:
            return
        available = self._available(bankroll)
        if available < cfg.min_stake_usdt:
            return

        reading = self._vol.regime()
        for raw in self._client.open_rounds():
            if any(k[0] == raw.symbol for k in self._positions):
                continue
            if raw.strike is None:
                continue
            seconds_left = raw.seconds_remaining(self._client.now_ms())
            spot = self._client.spot_price(raw.symbol)
            sigma = self._vol.sigma_annual(raw.symbol)
            reading = self._vol.regime(raw.symbol)

            block_sigma = sigma * math.sqrt(
                cfg.round_seconds / (365.0 * 24.0 * 60.0 * 60.0))
            excursion = (abs(spot - raw.strike) / raw.strike / block_sigma
                         if block_sigma > 0 else 0.0)
            travelled = (abs(spot - raw.strike) / raw.strike / block_sigma
                         if block_sigma > 0 else 0.0)

            reason = self._pin_gate(cfg, reading, seconds_left, excursion,
                                    travelled)
            if reason is not None:
                self._watching[raw.topic_id] = (raw.end_ms, reason)
                continue

            side = Side.UP if spot > raw.strike else Side.DOWN
            price = self._book_price(raw, side)
            if price is None:
                self._watching[raw.topic_id] = (raw.end_ms,
                                                "no executable quote")
                continue
            if not (cfg.min_entry_price <= price <= cfg.max_entry_price):
                self._watching[raw.topic_id] = (raw.end_ms,
                                                "price outside the band")
                continue
            if not clears_return(price, raw.fee_bps, cfg):
                self._watching[raw.topic_id] = (
                    raw.end_ms, "win pays less than the return floor")
                continue

            stake = min(available, bankroll * cfg.max_stake_pct)
            if stake < cfg.min_stake_usdt:
                self._watching[raw.topic_id] = (raw.end_ms,
                                                "stake below the venue minimum")
                continue

            sig = Signal(side=side, model_prob=price, fill_price=price,
                         edge=0.0, stake_usdt=stake,
                         seconds_left=seconds_left, buffer_z=excursion)
            LOG.info("PIN %s %s @ %.3f pays %.0f%% stake %.2f (%.0fs left) "
                     "-- %s", raw.slug, side.value, price,
                     win_return(price, raw.fee_bps) * 100, stake,
                     seconds_left, reading.describe())
            order_id = self._place_leg(raw, side, price, stake, mode)
            tid = self._journal.record(mode, raw, sig, spot, sigma, bankroll,
                                       order_id, profile=cfg.profile_name)
            self._seen[raw.topic_id] = raw.end_ms
            self._positions[(raw.symbol, side)] = Position(
                tid, raw, sig, stake, 1, profile=cfg.profile_name,
                entry_cfg=cfg)
            available -= stake
            if (len(self._positions) >= cfg.max_concurrent_positions
                    or available < cfg.min_stake_usdt):
                return
```

Note on `model_prob`: it is set to the fill price rather than a model output, because there is no model probability on this path and recording a fabricated one would corrupt `RiskManager`'s Poisson-binomial calibration accumulator. Setting it equal to the price makes the trade calibration-neutral — it contributes exactly the expectation the price implies.

If `_place_leg`'s signature in the straddle path does not match this call, adapt the call to it; that helper is the existing single place where paper and live order placement diverge, and pin must not introduce a second one.

- [ ] **Step 6: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Back the side spot already sits on when the market has stopped moving"
```

---

### Task 10: Dispatch through the playbook

**Files:**
- Modify: `btc_5m_predictor.py` — `Trader.__init__` (add the resolution cache), `Trader._maybe_enter` (line 4438)
- Test: `test_btc_5m.py` — append `TestPlaybookDispatch`

**Interfaces:**
- Consumes: `PLAYBOOKS`, `resolve_playbook_entry`, `_maybe_enter_straddle`, `_maybe_enter_model`, `_maybe_enter_pin`
- Produces: `Trader._effective(profile: str, overlay: dict) -> Config`, and a rewritten `Trader._maybe_enter(bankroll: float, mode: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestPlaybookDispatch(unittest.TestCase):

    def _trader(self, profile):
        doc = m.default_config_document(profile)
        cfg = m.build_config(doc, api_key="k", api_secret="s", live=False,
                             db_path=":memory:", profile=profile)
        trader = object.__new__(m.Trader)
        trader._static_cfg = cfg
        trader._store = None
        trader._positions = {}
        trader._effective_base = None
        trader._effective_cache = {}
        trader._calls = []
        return trader

    def _stub_paths(self, trader):
        trader._maybe_enter_straddle = lambda b, mo, cfg=None: (
            trader._calls.append(("straddle", cfg.profile_name if cfg
                                  else None)))
        trader._maybe_enter_model = lambda b, mo, cfg=None: (
            trader._calls.append(("model", cfg.profile_name if cfg else None)))
        trader._maybe_enter_pin = lambda b, mo, cfg=None: (
            trader._calls.append(("pin", cfg.profile_name if cfg else None)))

    def test_a_pinned_profile_never_consults_the_playbook(self):
        trader = self._trader("buffer")
        self._stub_paths(trader)
        trader._maybe_enter(100.0, "PAPER")
        self.assertEqual(trader._calls, [("model", None)])

    def test_a_pinned_straddle_profile_dispatches_to_straddle(self):
        trader = self._trader("straddle")
        self._stub_paths(trader)
        trader._maybe_enter(100.0, "PAPER")
        self.assertEqual(trader._calls, [("straddle", None)])

    def test_adaptive_walks_the_playbook_in_order(self):
        trader = self._trader("adaptive")
        self._stub_paths(trader)
        trader._regime_reading = lambda: m.RegimeReading(current="BIASED")
        trader._maybe_enter(100.0, "PAPER")
        self.assertEqual(trader._calls,
                         [("model", "buffer"), ("straddle", "straddle")])

    def test_adaptive_stops_at_the_first_entry_that_opens(self):
        trader = self._trader("adaptive")
        self._stub_paths(trader)
        trader._regime_reading = lambda: m.RegimeReading(current="FLAT")

        def opener(b, mo, cfg=None):
            trader._calls.append(("straddle", cfg.profile_name))
            trader._positions[("BTCUSDT", m.Side.UP)] = "sentinel"

        trader._maybe_enter_straddle = opener
        trader._maybe_enter(100.0, "PAPER")
        self.assertEqual(trader._calls, [("straddle", "straddle")])

    def test_an_unknown_regime_trades_nothing(self):
        trader = self._trader("adaptive")
        self._stub_paths(trader)
        trader._regime_reading = lambda: m.RegimeReading(current="UNKNOWN")
        trader._maybe_enter(100.0, "PAPER")
        self.assertEqual(trader._calls, [])

    def test_resolution_is_cached_per_entry(self):
        trader = self._trader("adaptive")
        first = trader._effective("buffer", {})
        second = trader._effective("buffer", {})
        self.assertIs(first, second)

    def test_different_overlays_resolve_to_different_configs(self):
        trader = self._trader("adaptive")
        plain = trader._effective("straddle", {})
        loose = trader._effective("straddle",
                                  {"straddle_first_leg_max_price": 0.30})
        self.assertNotEqual(plain.straddle_first_leg_max_price,
                            loose.straddle_first_leg_max_price)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestPlaybookDispatch -v`
Expected: FAIL with `AttributeError: 'Trader' object has no attribute '_effective'`

- [ ] **Step 3: Add the cache to `Trader.__init__`**

In `Trader.__init__`, immediately after `self._positions: dict[tuple[str, Side], Position] = {}`, add:

```python
        # Resolved playbook configs, rebuilt whenever the base config swaps.
        # Building a frozen Config re-runs its validation, which is cheap but
        # pointless to repeat on every poll.
        self._effective_base: Config | None = None
        self._effective_cache: dict[str, Config] = {}
```

- [ ] **Step 4: Write `_effective` and rewrite `_maybe_enter`**

Replace the whole of `Trader._maybe_enter` with:

```python
    def _effective(self, profile: str, overlay: dict) -> Config:
        """The config for one playbook entry, cached until the base swaps."""
        base = self._cfg
        if self._effective_base is not base:
            self._effective_base = base
            self._effective_cache = {}
        key = f"{profile}|{sorted(overlay.items())}"
        cached = self._effective_cache.get(key)
        if cached is None:
            cached = resolve_playbook_entry(base, profile, overlay)
            self._effective_cache[key] = cached
        return cached

    def _regime_reading(self) -> RegimeReading:
        return self._vol.regime()

    def _dispatch_entry(self, cfg: Config, bankroll: float,
                        mode: str) -> None:
        """Send one config to whichever entry path it selects."""
        if cfg.straddle:
            self._maybe_enter_straddle(bankroll, mode, cfg)
        elif cfg.pin:
            self._maybe_enter_pin(bankroll, mode, cfg)
        else:
            self._maybe_enter_model(bankroll, mode, cfg)

    def _maybe_enter(self, bankroll: float, mode: str) -> None:
        """
        Dispatch to whichever entry strategy applies right now.

        Without `adaptive` this is what it always was: one profile, one
        strategy, chosen at startup.

        With it, the regime selects an ordered list of entries and each is
        offered the round in turn. The first to open a position ends the
        pass -- not as an optimisation, but because the alternatives are
        different bets on the same round and taking two of them is a
        position nobody chose.
        """
        cfg = self._cfg
        if not cfg.adaptive:
            self._dispatch_entry(cfg, bankroll, mode)
            return

        reading = self._regime_reading()
        for profile, overlay in PLAYBOOKS.get(reading.current, ()):
            before = len(self._positions)
            self._dispatch_entry(self._effective(profile, overlay),
                                 bankroll, mode)
            if len(self._positions) > before:
                return
```

- [ ] **Step 5: Give the two existing entry paths an optional config argument**

Change `_maybe_enter_straddle(self, bankroll: float, mode: str)` to `_maybe_enter_straddle(self, bankroll: float, mode: str, cfg: Config | None = None)` and bind `cfg = cfg or self._cfg` as its first statement, then replace every `self._cfg` reference inside the method body with `cfg`. Do the same for `_maybe_enter_model`. This is a mechanical substitution; do not change any logic.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestPlaybookDispatch -v`
Expected: PASS, 7 tests

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Offer each round to the strategies the market's state actually suits"
```

---

### Task 11: Manage an open position by the regime, not by the current profile

**Files:**
- Modify: `btc_5m_predictor.py` — `Trader._maybe_scale_in_all` (line 5202), plus a new `_position_action` static method and a new `_maybe_hedge` method
- Test: `test_btc_5m.py` — append `TestOpenPositionPolicy`

**Interfaces:**
- Consumes: `RegimeReading`, `Trend`, `Position`, `straddle_completion_stake`
- Produces: `Trader._position_action(reading: RegimeReading, trend: Trend, side: Side, lock_in: bool) -> str` returning one of `"hedge"`, `"scale_in"`, `"hold"`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestOpenPositionPolicy(unittest.TestCase):
    """
    The decision table for a position that is already open.

    The central finding it encodes: hedging at market prices is EV-neutral
    before fees and EV-negative after them, so a hedge is only worth placing
    when it locks in money that exists, or when the regime says the quote is
    wrong about the adverse side.
    """

    def _trend(self, direction):
        return m.Trend(direction=direction, impulse=2.0, z=1.5,
                       efficiency=0.8, run=2, decay=0.9, rounds_left=2.0,
                       phase="running")

    def test_a_lock_in_is_always_taken(self):
        for state in ("FLAT", "BIASED", "SWINGY", "UNKNOWN"):
            action = m.Trader._position_action(
                m.RegimeReading(current=state), self._trend(-1),
                m.Side.UP, lock_in=True)
            self.assertEqual(action, "hedge")

    def test_swingy_holds_rather_than_paying_fees_to_cancel_its_own_signal(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="SWINGY"), self._trend(-1),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "hold")

    def test_biased_with_the_trend_scales_in(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="BIASED"), self._trend(1),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "scale_in")

    def test_biased_against_the_trend_hedges(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="BIASED"), self._trend(-1),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "hedge")

    def test_flat_holds(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="FLAT"), self._trend(-1),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "hold")

    def test_a_directionless_trend_in_biased_holds(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="BIASED"), m.Trend(),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "hold")

    def test_unknown_regime_holds(self):
        action = m.Trader._position_action(
            m.RegimeReading(current="UNKNOWN"), self._trend(1),
            m.Side.UP, lock_in=False)
        self.assertEqual(action, "hold")

    def test_hedging_an_adverse_leg_at_market_is_ev_negative(self):
        # The regression that justifies the table. Buffer leg of 10.00 at
        # 0.65, other side now 0.65; larger hedges must not improve EV.
        fee, stake, opened = 200, 10.0, 0.65
        evs = []
        for hedge in (0.0, 2.0, 5.0):
            win_open = m.settle_pnl(stake, opened, True, fee) - hedge
            win_other = (m.settle_pnl(hedge, 0.65, True, fee) - stake
                         if hedge > 0 else -stake)
            evs.append(0.35 * win_open + 0.65 * win_other)
        self.assertLess(evs[1], evs[0])
        self.assertLess(evs[2], evs[1])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestOpenPositionPolicy -v`
Expected: FAIL with `AttributeError: type object 'Trader' has no attribute '_position_action'`

- [ ] **Step 3: Write the decision table**

Insert into `class Trader`, immediately before `_maybe_scale_in_all`:

```python
    @staticmethod
    def _position_action(reading: RegimeReading, trend: Trend, side: Side,
                         lock_in: bool) -> str:
        """
        What to do with a position that is already open: hedge, scale in, or
        hold.

        WHY THIS IS NOT "HEDGE WHEN WORRIED"
        ------------------------------------
        Hedging an open leg at the prices actually on offer is EV-neutral
        before fees and EV-negative after them. It does not recover a loss;
        it converts a wide distribution into a narrow one and pays the venue
        for the conversion. Measured, not assumed: a 10.00 leg at 0.65 with
        the other side back at 0.65 returns -4.653 unhedged, -4.667 hedged
        with 2.00 and -4.688 hedged with 5.00.

        So there are exactly two reasons to hedge, and "the market looks
        frightening" is neither of them:

        * `lock_in` -- straddle_completion_stake found a stake where BOTH
          outcomes pay more than the round cost. That happens only when the
          two fill prices, from their two different moments, sum to under
          one, which is arbitrage rather than a variance trade.
        * the regime says the quote is wrong about the adverse side. In
          BIASED the trend continues, so the side we are not on is likelier
          than its price implies and the hedge is genuinely EV-positive.

        SWINGY is the case that looks most alarming and must NOT hedge.
        SWINGY means spot crosses back. The venue prices our leg off its
        current distance from the strike; the regime says that distance
        mean-reverts. Hedging there pays fees to cancel the very signal that
        fired.

        Scale-in survives only in BIASED-with-the-trend, which is the one
        state its "press it while the market has inertia" premise was ever
        true in.
        """
        if lock_in:
            return "hedge"
        if reading.current == "BIASED" and trend.direction != 0:
            return "scale_in" if trend.favours(side) else "hedge"
        return "hold"
```

- [ ] **Step 4: Gate scale-in on the position's own profile**

Replace `Trader._maybe_scale_in_all` with:

```python
    def _maybe_scale_in_all(self, bankroll: float) -> None:
        """
        Top up, hedge or leave alone -- decided per position, not per config.

        The per-position part matters under `adaptive`: the regime can change
        while a position is open, and the strategy chosen for the next round
        has no business managing this one. A straddle leg is never topped up
        no matter what the market has since become, because the strategy that
        opened it has no model probability to top up toward.
        """
        for key in list(self._positions):
            pos = self._positions.get(key)
            if pos is None:
                continue
            cfg = pos.entry_cfg or self._cfg
            if cfg.straddle:
                continue
            reading = self._vol.regime(pos.rnd.symbol)
            trend = self._vol.trend(pos.rnd.symbol)
            lock_in = self._lock_in_available(pos)
            action = self._position_action(reading, trend,
                                           pos.signal.side, lock_in)
            if action == "scale_in":
                self._maybe_scale_in(bankroll, key)
            elif action == "hedge":
                self._maybe_hedge(bankroll, key, guaranteed=lock_in)
```

- [ ] **Step 5: Write the lock-in probe and the hedge**

Insert into `class Trader`, immediately after `_maybe_scale_in_all`:

```python
    def _lock_in_available(self, pos: Position) -> bool:
        """Would completing this leg pay more than the round cost, either way?"""
        other = pos.signal.side.other
        price = self._book_price(pos.rnd, other)
        if price is None or not 0.0 < price < 1.0:
            return False
        _, guaranteed = straddle_completion_stake(
            pos.committed_usdt, pos.signal.fill_price, price,
            pos.rnd.fee_bps, budget=self._available(self._bankroll()))
        return guaranteed

    def _maybe_hedge(self, bankroll: float, key: tuple[str, Side],
                     guaranteed: bool) -> None:
        """
        Buy the other side of a round we are already in.

        Sized by straddle_completion_stake, which equalises the two payouts
        and therefore maximises the guaranteed profit when one exists. When
        one does not, the same stake is the loss-capping hedge -- and this is
        only reached in BIASED-against-us, where the trend continuing makes
        the adverse side likelier than its price implies.
        """
        pos = self._positions.get(key)
        if pos is None:
            return
        other = pos.signal.side.other
        if (pos.rnd.symbol, other) in self._positions:
            return
        price = self._book_price(pos.rnd, other)
        if price is None or not 0.0 < price < 1.0:
            return
        cfg = pos.entry_cfg or self._cfg
        budget = self._available(bankroll)
        stake, locked = straddle_completion_stake(
            pos.committed_usdt, pos.signal.fill_price, price,
            pos.rnd.fee_bps, budget)
        if stake < cfg.min_stake_usdt or stake > budget:
            return

        seconds_left = pos.rnd.seconds_remaining(self._client.now_ms())
        LOG.info("HEDGE %s %s @ %.3f stake %.2f (%s, %.0fs left)",
                 pos.rnd.slug, other.value, price, stake,
                 "locks the round in" if locked else "caps the loss",
                 seconds_left)
        mode = "LIVE" if self._live else "PAPER"
        order_id = self._place_leg(pos.rnd, other, price, stake, mode)
        sig = Signal(side=other, model_prob=price, fill_price=price,
                     edge=0.0, stake_usdt=stake, seconds_left=seconds_left)
        tid = self._journal.record(
            mode, pos.rnd, sig, pos.rnd.strike or 0.0, 0.0, bankroll,
            order_id, profile=f"{pos.profile}-hedge")
        self._positions[(pos.rnd.symbol, other)] = Position(
            tid, pos.rnd, sig, stake, 1, profile=f"{pos.profile}-hedge",
            entry_cfg=cfg)
```

Confirm `Side.other` exists — it is defined at line 1252. If `_place_leg`'s signature differs from the call above, adapt the call; do not add a second order-placement path.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestOpenPositionPolicy -v`
Expected: PASS, 8 tests

- [ ] **Step 7: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 8: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Hedge only when it locks money in or the trend says the quote is wrong"
```

---

### Task 12: Say the market's state out loud, and validate the playbooks at startup

**Files:**
- Modify: `btc_5m_predictor.py` — `Trader.run` (line 4323, the startup log block and the main loop), `preflight` (line 5615)
- Test: `test_btc_5m.py` — append `TestRegimeLogging`

**Interfaces:**
- Consumes: `RegimeReading.describe`, `RegimeReading.detail`, `validate_playbooks`
- Produces: `Trader._log_regime() -> None`

- [ ] **Step 1: Write the failing tests**

Append to `test_btc_5m.py`:

```python
class TestRegimeLogging(unittest.TestCase):

    def _trader(self):
        doc = m.default_config_document("adaptive")
        cfg = m.build_config(doc, api_key="k", api_secret="s", live=False,
                             db_path=":memory:", profile="adaptive")
        trader = object.__new__(m.Trader)
        trader._static_cfg = cfg
        trader._store = None
        trader._last_regime = None
        return trader

    def test_logs_once_when_the_state_changes(self):
        trader = self._trader()
        readings = iter([
            m.RegimeReading(current="FLAT", commit_threshold=3),
            m.RegimeReading(current="FLAT", commit_threshold=3),
            m.RegimeReading(current="BIASED", previous="FLAT",
                            previous_age_min=40.0, commit_threshold=3),
        ])
        trader._regime_reading = lambda: next(readings)
        with self.assertLogs(m.LOG, level="INFO") as captured:
            trader._log_regime()
            trader._log_regime()
            trader._log_regime()
        lines = [r for r in captured.output if "MARKET" in r]
        self.assertEqual(len(lines), 2)
        self.assertIn("currently BIASED", lines[1])

    def test_logs_when_a_transition_starts(self):
        trader = self._trader()
        readings = iter([
            m.RegimeReading(current="FLAT", commit_threshold=3),
            m.RegimeReading(current="FLAT", entering="SWINGY",
                            entering_count=1, commit_threshold=3),
        ])
        trader._regime_reading = lambda: next(readings)
        with self.assertLogs(m.LOG, level="INFO") as captured:
            trader._log_regime()
            trader._log_regime()
        lines = [r for r in captured.output if "MARKET" in r]
        self.assertEqual(len(lines), 2)
        self.assertIn("entering SWINGY (1/3)", lines[1])

    def test_shipped_playbooks_pass_startup_validation(self):
        m.validate_playbooks()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest test_btc_5m.TestRegimeLogging -v`
Expected: FAIL with `AttributeError: 'Trader' object has no attribute '_log_regime'`

- [ ] **Step 3: Add the tracking field and the logger**

In `Trader.__init__`, immediately after the `_effective_cache` lines added in Task 10, add:

```python
        # The last regime sentence printed, so the state is announced when it
        # changes rather than on every poll.
        self._last_regime: str | None = None
```

Insert into `class Trader`, immediately after `_regime_reading`:

```python
    def _log_regime(self) -> None:
        """
        Announce the market's state when it changes, and stay quiet otherwise.

        Printed on every poll this would be noise; printed never, a switch of
        strategy would look like the bot changing its mind for no reason. The
        measurements go out with it so the label can be argued with.
        """
        reading = self._regime_reading()
        sentence = reading.describe()
        if sentence == self._last_regime:
            return
        self._last_regime = sentence
        LOG.info("%s", sentence)
        LOG.info("  %s", reading.detail())
```

- [ ] **Step 4: Call it from the loop and validate at startup**

In `Trader.run`, immediately before the `self._maybe_enter(bankroll, ...)` call in the main loop, add:

```python
                    if self._cfg.adaptive:
                        self._log_regime()
```

In `Trader.run`, immediately before `self._account_risk = RiskManager(...)`, add:

```python
            if self._cfg.adaptive:
                # Fail at startup rather than deep inside an entry attempt.
                validate_playbooks()
                LOG.info("Adaptive: regime selects the strategy. Day limits "
                         "pinned at loss %.0f%%, %d consecutive losses, %d "
                         "rounds.", self._cfg.daily_loss_limit_pct * 100,
                         self._cfg.max_consecutive_losses,
                         self._cfg.max_rounds_per_day)
```

In `preflight`, add a check alongside the existing ones:

```python
    check("playbooks", lambda: (validate_playbooks() or "all entries valid"))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m unittest test_btc_5m.TestRegimeLogging -v`
Expected: PASS, 3 tests

- [ ] **Step 6: Run the whole suite**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py
git commit -m "Say which market we think we are in, and check the playbooks before trading"
```

---

### Task 13: The backtest harness

**Files:**
- Create: `backtest_regime.py`
- Test: `backtest_regime.py --selftest` (matching `flow.py`'s convention of carrying its own no-network self-test)

**Interfaces:**
- Consumes: `measure_round_shape`, `classify_regime`, `RegimeTracker`, `Config`, `breakeven_probability`, `win_return`, `default_config_document`, `build_config`
- Produces: a standalone CLI. Functions: `fetch_klines(symbol, start_ms, end_ms, cache_path) -> list[tuple[int, float]]`, `blocks(closes, block) -> list[list[float]]`, `walk(shapes, cfg) -> list[RegimeReading]`, `score_pin(...) -> dict`, `max_profitable_fill(win_rate, fee_bps) -> float`

**What this can and cannot prove — read before writing a line of it.**

The venue publishes no historical price archive. Its only history endpoints are `settled_history` and `order_history`, which are the account's own past positions. Binance klines give **spot**, never what UP and DOWN were quoted at. Therefore:

- **P&L over the period cannot be computed.** Any figure would come from `digital_up_probability` pricing the fills that same model chose — the model marking its own homework. This tool must refuse to print one.
- **`straddle` cannot be tested at all here.** Its premise is two quotes at two different moments, and no quote history exists.
- **`pin`'s win rate is fully real.** Its decision is "which side is spot on at T−75s" and its outcome is "which side did it close on". Both come from klines and neither needs a venue price.
- **The regime labels are real**, and can be read against direct observation of the same period.

So the tool reports win rate, trade count, and the **maximum fill price at which that win rate is profitable**. That last number is the actionable one: it converts an untestable P&L into a threshold you can hold against the live book.

- [ ] **Step 1: Write the tool with its self-test**

Create `backtest_regime.py`:

```python
#!/usr/bin/env python3
"""
Replay the regime detector and the pin strategy over historical 1m closes.

WHAT THIS PROVES, AND WHAT IT CANNOT
------------------------------------
The venue publishes no historical price archive -- its only history
endpoints are the account's own settled positions -- so what UP and DOWN
were quoted at during a round two weeks ago is simply not recoverable.
Binance klines give SPOT and nothing else.

That draws a hard line through this tool:

  * P&L is NOT computed, and no option will make it. Any figure would come
    from pricing the fills with the same model that chose them, which is the
    model marking its own homework and would produce a flattering number by
    construction.
  * The straddle strategy is NOT tested. Its premise is two quotes at two
    different moments; without quote history there is nothing to test.
  * pin's win rate IS real. Its decision is "which side is spot on with
    75 seconds left" and its outcome is "which side did the round close
    on". Both come from the closes. No venue price is involved anywhere.
  * The regime labels are real, and are printed as a timeline so they can
    be read against what you remember of the period.

What replaces P&L is a threshold: given a measured win rate, the maximum
fill price at which that win rate is profitable. That is a number you can
hold against the live book.

    python3 backtest_regime.py --days 14
    python3 backtest_regime.py --days 14 --symbol ETHUSDT
    python3 backtest_regime.py --selftest        # no network
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time

import requests

import btc_5m_predictor as m

BASE = "https://api.binance.com"
MAX_LIMIT = 1000


def fetch_klines(symbol: str, start_ms: int, end_ms: int,
                 cache_path: str | None) -> list[tuple[int, float]]:
    """
    (open_time_ms, close) for every 1m candle in the window, oldest first.

    Paginated because Binance caps a request at 1000 candles and two weeks
    is 20160 of them. Cached to disk because tuning thresholds means running
    this many times and re-downloading the same fortnight each time is rude
    to the endpoint and slow for the operator.
    """
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as fh:
            return [(int(t), float(c)) for t, c in json.load(fh)]

    session = requests.Session()
    out: list[tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        resp = session.get(
            BASE + "/api/v3/klines",
            params={"symbol": symbol, "interval": "1m",
                    "startTime": cursor, "endTime": end_ms,
                    "limit": MAX_LIMIT},
            timeout=20)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            out.append((int(row[0]), float(row[4])))
        nxt = int(rows[-1][0]) + 60_000
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(0.15)

    if cache_path:
        with open(cache_path, "w") as fh:
            json.dump(out, fh)
    return out


def blocks(rows: list[tuple[int, float]], block: int
           ) -> list[list[tuple[int, float]]]:
    """
    Cut into round-length blocks aligned to the wall clock.

    Rounds open on the clock -- :00, :05, :10 -- so blocks are cut on the
    same boundaries rather than on an arbitrary offset from the first
    candle. A misaligned cut would measure a strike the venue never used.
    Each block carries block+1 samples: its open, which is the strike, then
    every close through to the round's end.
    """
    if not rows:
        return []
    step_ms = 60_000 * block
    out: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for ts, close in rows:
        if ts % step_ms == 0 and current:
            current.append((ts, close))
            out.append(current)
            current = [(ts, close)]
        else:
            current.append((ts, close))
    return [b for b in out if len(b) == block + 1]


def max_profitable_fill(win_rate: float, fee_bps: int) -> float:
    """
    Highest price at which `win_rate` still makes money, to three decimals.

    Bisection on breakeven_probability, which is monotone in price. Returns
    0.0 when even the cheapest contract cannot clear the win rate.
    """
    lo, hi = 0.001, 0.999
    if m.breakeven_probability(lo, fee_bps) >= win_rate:
        return 0.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if m.breakeven_probability(mid, fee_bps) < win_rate:
            lo = mid
        else:
            hi = mid
    return round(lo, 3)


def walk(shaped: list[m.RoundShape], cfg: m.Config,
         stamps: list[int]) -> list[m.RegimeReading]:
    """
    The reading as it stood entering each round, using only earlier rounds.

    Strictly causal: round i is classified from rounds before i and never
    from i itself. A backtest that let a round inform its own label would be
    reporting hindsight as skill.
    """
    tracker = m.RegimeTracker(cfg.regime_commit_readings)
    readings: list[m.RegimeReading] = []
    for i in range(len(shaped)):
        history = shaped[:i]
        if len(history) < cfg.regime_recent_rounds + cfg.regime_min_rounds:
            readings.append(tracker.reading)
            continue
        recent = history[-cfg.regime_recent_rounds:]
        baseline = history[:-cfg.regime_recent_rounds]
        label, measures = m.classify_regime(recent, baseline, cfg)
        readings.append(tracker.update(label, stamps[i] / 1000.0, measures))
    return readings


def score_pin(chunks: list[list[tuple[int, float]]],
              shaped: list[m.RoundShape],
              readings: list[m.RegimeReading],
              cfg: m.Config, sigma_block: float, fee_bps: int) -> dict:
    """
    pin's real win rate: side chosen with 75s left, outcome at the close.

    At 1m resolution the only sample inside a 75-second window is the one
    with 60 seconds left, so each qualifying round contributes exactly one
    decision. That is coarser than the live bot, which polls every two
    seconds, and it is coarse in pin's DISFAVOUR -- the live bot sees the
    price nearer the close and therefore has more information, not less.
    """
    wins = trades = 0
    blocked: dict[str, int] = {}
    for chunk, shape, reading in zip(chunks, shaped, readings):
        strike = chunk[0][1]
        decision = chunk[-2][1]      # 60 seconds left
        final = chunk[-1][1]
        if strike <= 0 or sigma_block <= 0:
            continue
        excursion = abs(decision - strike) / strike / sigma_block
        # Travel up to the decision point only. Using the whole round would
        # be reading the future.
        seen = [c for _, c in chunk[:-1]]
        travelled = m.measure_round_shape(seen, sigma_block).travel
        reason = m.Trader._pin_gate(cfg, reading, 60.0, excursion, travelled)
        if reason is not None:
            blocked[reason] = blocked.get(reason, 0) + 1
            continue
        trades += 1
        up = decision > strike
        if (final > strike) == up:
            wins += 1

    rate = wins / trades if trades else 0.0
    # Standard error of a proportion; a win rate without one invites reading
    # nine trades as a result.
    se = math.sqrt(rate * (1 - rate) / trades) if trades > 1 else 0.0
    return {"trades": trades, "wins": wins, "win_rate": rate,
            "std_error": se,
            "max_profitable_fill": max_profitable_fill(rate, fee_bps),
            "blocked": blocked}


def report(rows: list[tuple[int, float]], cfg: m.Config, fee_bps: int) -> int:
    block = max(1, round(cfg.round_seconds / 60.0))
    chunks = blocks(rows, block)
    if len(chunks) < cfg.regime_recent_rounds + cfg.regime_min_rounds + 10:
        print(f"Only {len(chunks)} complete rounds; need more history.")
        return 1

    closes = [c for _, c in rows]
    steps = [math.log(b / a) for a, b in zip(closes, closes[1:])
             if a > 0 and b > 0]
    sigma_block = statistics.pstdev(steps) * math.sqrt(block)

    shaped = [m.measure_round_shape([c for _, c in chunk], sigma_block)
              for chunk in chunks]
    stamps = [chunk[0][0] for chunk in chunks]
    readings = walk(shaped, cfg, stamps)

    counts: dict[str, int] = {}
    for r in readings:
        counts[r.current] = counts.get(r.current, 0) + 1
    total = len(readings)

    print(f"Rounds analysed : {total}  ({total * block / 60.0:.0f} hours)")
    print()
    print("Time spent in each state")
    for state in ("UNKNOWN", "FLAT", "BIASED", "SWINGY"):
        n = counts.get(state, 0)
        print(f"  {state:<8} {n:6d}  {n / total:6.1%}")

    changes = sum(1 for a, b in zip(readings, readings[1:])
                  if a.current != b.current)
    print(f"\nState changes   : {changes} "
          f"(about one every {total / max(changes, 1) * block / 60.0:.1f} hours)")

    pin = score_pin(chunks, shaped, readings, cfg, sigma_block, fee_bps)
    print("\npin, on real outcomes")
    print(f"  rounds taken        : {pin['trades']}")
    if pin["trades"]:
        print(f"  win rate            : {pin['win_rate']:.1%} "
              f"+/- {pin['std_error']:.1%}")
        print(f"  profitable at fills : below {pin['max_profitable_fill']:.3f}")
        print(f"  configured ceiling  : {cfg.max_entry_price:.3f}")
        verdict = ("the ceiling is inside the profitable band"
                   if cfg.max_entry_price <= pin["max_profitable_fill"]
                   else "THE CEILING IS ABOVE THE PROFITABLE BAND -- lower "
                        "max_entry_price or this loses money")
        print(f"  verdict             : {verdict}")
    if pin["blocked"]:
        print("  rounds refused, by gate:")
        for reason, n in sorted(pin["blocked"].items(), key=lambda kv: -kv[1]):
            print(f"    {n:6d}  {reason}")

    print("\nNOT MEASURED, and not measurable from this data:")
    print("  * P&L. The venue publishes no price history, so any figure")
    print("    would price the fills with the model that chose them.")
    print("  * straddle. Its premise is two quotes at two moments, and no")
    print("    quote history exists.")
    return 0


def selftest() -> int:
    """No network. Asserts the parts that carry the arithmetic."""
    rows = [(i * 60_000, 100.0) for i in range(31)]
    chunks = blocks(rows, 5)
    assert chunks, "wall-clock aligned blocks should be produced"
    assert all(len(c) == 6 for c in chunks), "each block carries open + 5"
    assert all(c[0][0] % 300_000 == 0 for c in chunks), "blocks start on :00/:05"

    # A win rate exactly at a price's breakeven must not be called profitable.
    be = m.breakeven_probability(0.80, 200)
    assert max_profitable_fill(be, 200) <= 0.801
    assert max_profitable_fill(0.99, 200) > 0.90
    assert max_profitable_fill(0.01, 200) == 0.0

    # walk must be strictly causal: the first rounds have no verdict.
    cfg = m.Config(api_key="k", api_secret="s")
    shaped = [m.RoundShape(travel=1.0, net=0.1, straightness=0.5,
                           terminal=0.1, crossings=1) for _ in range(60)]
    readings = walk(shaped, cfg, [i * 300_000 for i in range(60)])
    assert readings[0].current == "UNKNOWN"
    assert len(readings) == len(shaped)

    print("selftest OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--profile", default="pin")
    ap.add_argument("--fee-bps", type=int, default=200)
    ap.add_argument("--cache", default=None,
                    help="path to cache the klines in (default: auto)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days * 24 * 60 * 60 * 1000)
    cache = args.cache or f".klines-{args.symbol}-{int(args.days)}d.json"
    print(f"Fetching {args.days:.0f} days of 1m {args.symbol} "
          f"(cache: {cache})...")
    rows = fetch_klines(args.symbol, start_ms, end_ms, cache)
    print(f"{len(rows)} candles.\n")

    doc = m.default_config_document(args.profile)
    cfg = m.build_config(doc, api_key="x", api_secret="x", live=False,
                         db_path=":memory:", profile=args.profile)
    return report(rows, cfg, args.fee_bps)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the self-test to verify it passes**

Run: `python backtest_regime.py --selftest`
Expected: `selftest OK`

- [ ] **Step 3: Add the cache file to .gitignore**

Run:

```bash
printf '\n# Cached klines for backtest_regime.py\n.klines-*.json\n' >> .gitignore
```

- [ ] **Step 4: Commit**

```bash
git add backtest_regime.py .gitignore
git commit -m "Replay the detector over real closes, and refuse to invent a P&L"
```

---

### Task 14: Run the backtest, tune from what it says, and confirm the live setting

**Files:**
- Modify: `btc_5m_predictor.py` — only if the backtest gives a stated reason to move a threshold
- Create: `docs/superpowers/backtest-2026-09-07.md`

**Interfaces:**
- Consumes: everything above
- Produces: a written result

- [ ] **Step 1: Run the full suite one final time**

Run: `python -m unittest test_btc_5m 2>&1 | tail -5`
Expected: OK

- [ ] **Step 2: Run the backtest over two weeks**

Run: `python backtest_regime.py --days 14`

Record the whole output.

- [ ] **Step 3: Read the result against the honest criterion**

The criterion is **not** "90% win rate". A fairly priced contract wins at roughly its own breakeven rate, so a 90% win rate bought at 0.90 returns −0.2% per unit staked and is worse than not trading. The criterion is the one `Journal.diagnose` already uses: **realised win rate against the breakeven for the price paid.**

Read three numbers together:

1. `win rate +/- std_error`. A win rate without its trade count is not a result.
2. `profitable at fills : below X`. This is the actionable output — the price ceiling the live book must beat.
3. `verdict`. If the configured `max_entry_price` sits above X, pin loses money as configured and the ceiling must come down.

- [ ] **Step 4: Tune only where the output gives a reason**

Permitted changes, each requiring the output line that motivates it to be quoted in the write-up:

- `max_entry_price` lowered if the verdict says the ceiling is above the profitable band.
- A regime threshold moved if the state distribution is degenerate — one state holding more than ~85% of the time, or fewer than about 3 state changes across 14 days, means the bands are not separating anything.
- `pin_min_sigmas` or `pin_max_round_travel` moved if one gate is refusing more than ~90% of rounds by itself, which the `rounds refused, by gate` breakdown shows directly.

Forbidden: changing any threshold because the win rate is not yet 90%. Fourteen days is roughly 4000 rounds and pin fires on a fraction of them; tuning gates against that sample until a number reads well is fitting noise, and the resulting configuration would be worse live than the untuned one.

After any change, re-run Steps 1 and 2.

- [ ] **Step 5: Write up the result**

Create `docs/superpowers/backtest-2026-09-07.md` containing: the exact command run, the full output, every threshold changed with the output line that justified it, and a plain statement of what remains unmeasured (P&L, and straddle entirely).

- [ ] **Step 6: Confirm the live setting**

Run:

```bash
grep -n "live: bool" btc_5m_predictor.py && ls -la *.json 2>/dev/null
```

`Config.live` defaults to `True`, so the source ships live. Paper mode comes from either a config file on disk setting `"live": false` or the `--paper` flag. If a config JSON exists in the repo or at the configured `--config` path with `"live": false`, report it to the operator with the exact file and line rather than editing it silently — flipping a bot to live money is the operator's call to make knowingly, and the backtest above cannot measure P&L, so it does not constitute evidence that the strategy is profitable.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/backtest-2026-09-07.md btc_5m_predictor.py
git commit -m "Record what fourteen days of real closes say about the detector and pin"
```

---

## Self-Review

**Spec coverage.** Detector input and per-round measures → Tasks 1, 4. Classification with concrete thresholds → Task 2. Known crossing-undercount limitation → documented in Task 1's docstring and Task 13's `score_pin`. Hysteresis and the `previous / current / entering` sentence → Tasks 3, 12. Config split and the reason for it → Tasks 5, 6, 7. Declared envelope and the overlay prohibition → Task 6. `PLAYBOOKS` including the BIASED overlay and the FLAT time split → Task 6. Round-boundary-only resolution → Task 10. Double-entry hazard → Task 10, relying on the pre-existing per-symbol guard, covered by `test_adaptive_stops_at_the_first_entry_that_opens`. Orphaned scale-in → Tasks 8, 11. Hedging EV table and the decision table → Task 11. `pin` and all four gates → Tasks 5, 9. Journal per-trade profile → Task 8. Testing plan → distributed across every task. Verification plan → Tasks 13, 14.

**Placeholder scan.** No "TBD", no "handle edge cases", no "similar to Task N". Every code step carries the code. Task 14's tuning step is deliberately conditional but names the exact permitted changes and the exact output line each requires.

**Type consistency.** `RoundShape` fields `travel / net / straightness / terminal / crossings` used identically in Tasks 1, 2, 4, 13. `RegimeReading` fields used identically in Tasks 3, 4, 9, 11, 12, 13. `classify_regime` returns `tuple[str, dict]` in Task 2 and is consumed as such in Tasks 4 and 13. `RegimeTracker.update(label, now, measures)` matches its callers. `Trader._pin_gate` keyword names match between Task 9's tests, its definition, and Task 13's call. `resolve_playbook_entry(base, profile, overlay)` matches Task 10's `_effective`. `Position(profile=, entry_cfg=)` matches Tasks 8, 9, 11. `Journal.record(..., profile=)` matches Tasks 8, 9, 11.

**One deliberate coupling to watch during execution.** Task 10 requires mechanically threading a `cfg` parameter through `_maybe_enter_straddle` and `_maybe_enter_model`, both of which are long methods that read `self._cfg` in many places. That substitution is the single largest source of risk in this plan. Do it as its own commit if the diff grows past roughly 40 lines, and run the full suite before and after.
