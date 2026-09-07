# Market regime orchestration

**Date:** 2026-09-07
**Status:** approved design, not yet implemented
**Scope:** `btc_5m_predictor.py`, `test_btc_5m.py`

## The problem

The bot runs one strategy chosen at startup and runs it until stopped. That
strategy is then correct or incorrect for the whole session, because the
market is not one thing.

`straddle` buys the unfavourable side on the premise that spot comes back
across the strike. That premise is true in one market and false in two. Run in
a market that picks a side and stays there, the strategy is not merely
suboptimal, it is systematically buying the side that loses.

Three states, observed:

* **SWINGY** — spot crosses the strike repeatedly within a round. Straddle's
  premise holds and this is the state it was built for.
* **BIASED** — spot picks a side each round and stays there, usually ending
  far from the strike. `buffer` is the right strategy. The market does flip,
  but once or twice in the state, not round after round.
* **FLAT** — spot barely moves for minutes, hovers near the strike, and
  finishes on whichever side it was already on. Predictable, and neither
  existing strategy will trade it.

The bot has no way to say which of these it is in, so it cannot choose.

## What this is not

It is not price prediction, and no component here forecasts a future state.
The detector reads the rounds that have already happened and reports how their
character compares to the rounds before them. "Entering SWINGY" means *the
evidence has left the current state's band and has not yet settled in the new
one* — a divergence that has been measured, not a claim about the next round.

## Architecture

A regime detector, a playbook that maps regime to strategy, and a pinned risk
envelope so that switching strategies cannot move the day's loss budget.

### Why the config splits in two

`RiskManager._cfg` reads live from the `ConfigStore` on every check
(`btc_5m_predictor.py:2093`). If a regime switch swapped the whole profile,
`daily_loss_limit_pct` would change underneath a day that has already been
partly spent: switching `straddle` (0.50) to `buffer` (0.35) after a 40%
drawdown halts the bot instantly on a limit it was never trading under, and
switching the other way silently lifts a halt that had already tripped.

So the configuration divides by *what it is accounted against*:

* **Entry parameters** are decided per round — price bands, edge floors,
  buffer sigmas, straddle gates, entry windows, stake fractions. These move
  with the regime. That is the point of the project.
* **Governance parameters** are accounted against a **day** —
  `daily_loss_limit_pct`, `max_consecutive_losses`, `max_rounds_per_day`. A
  limit that moves mid-day is not a limit. These are pinned for the session.

The split falls out of where each is already read, so it needs no discipline
to maintain:

| component | reads |
| --- | --- |
| entry paths, `assess()` | **effective** config, resolved per round from the regime |
| `RiskManager` | **base** config, from `ConfigStore` — the regime never touches it |
| `VolatilityEstimator` | base config; its parameters are measurement, not strategy |

`ConfigStore` remains a disk abstraction. The regime is not disk. The two are
never merged.

### The envelope is declared, not derived

```python
ADAPTIVE_ENVELOPE = {
    "daily_loss_limit_pct": 0.50,
    "max_consecutive_losses": 10,
    "max_rounds_per_day": 400,
}
```

Deriving it — minimum across reachable profiles, say — would hand a straddle
day `buffer`'s 0.35 limit. Straddle loses roughly 20% of bankroll on a bad
round, so that halts after 1.75 of them: strictly worse than what the bot does
today, arrived at by a rule nobody wrote down. This file's comments make the
point repeatedly ("Stated rather than inherited so a change to the default
cannot silently reshape this"), and derivation is exactly that silent
reshaping.

0.50 is chosen, not defaulted: the worst single round across reachable
regimes is straddle's ~20%, so 0.50 buys 2.5 bad rounds — the same headroom
every other profile is given.

`max_consecutive_losses: 10` is the weakest number here and is recorded as
such. A straddle "loss" is a bad round; a buffer loss is a full stake. The
counter measures two different things under one name. Revisit once the paper
run produces streak data.

Overlays are **forbidden** from touching these three keys. Validated at
startup, raising `ValueError`. A silent win here would defeat the whole split.

## Component 1 — the detector

### Input: one-second closes, not one-minute

The first version of this spec fed the detector from the 500 one-minute closes
`sigma_annual` already fetches, on the grounds that it cost no extra request.
That was measured and it is wrong. Over 120 consecutive real rounds of
BTCUSDT, sampling the same rounds at 1m instead of 1s:

| measure | at 1s | at 1m | what 1m does to it |
| --- | --- | --- | --- |
| crossings per round | 2.52 | 0.78 | sees **31%** of them |
| travel per round | 0.00331 | 0.00120 | sees **36%** of it |
| straightness | 0.208 | 0.480 | **overstates by 2.3x** |

Thirteen of forty rounds crossed the strike but showed **zero** crossings at
1m. Both classification axes are therefore severely resolution-dependent, and
in the same direction: coarse sampling makes every market look straighter and
calmer than it is, which is precisely the error that would route straddle
money into a market that had stopped swinging.

So the detector reads **1-second klines**, on its own fetch, with its own
cache. This deliberately breaks the "same closes as the sigma" discipline that
`_measure_trend` follows, and the reason that discipline does not apply here
is that it exists to stop a trend being compared against a sigma from a
different moment. The regime is not compared against the sigma; it is an
independent measurement at a different timescale, and internal coherence is
all it needs.

**Cost, kept small by a rolling buffer.** The window is 24 recent rounds
compared against 96 baseline rounds — 120 rounds, 10 hours, 36000 one-second
candles. Seeding that costs 36 requests at Binance's 1000-candle limit, paid
once at startup. Afterwards the buffer is topped up with only the seconds
since the last refresh: **one request per minute** in steady state, at weight
2 against a 6000/minute IP budget.

The recent-against-baseline comparison is what makes "diverging" a measurement
rather than a threshold guess, and it is why the size thresholds below are
ratios rather than hardcoded BTC numbers that rot when the volatility regime
shifts.

### Per-round measures

The series is cut into aligned 5-minute blocks. The strike proxy is the block
open, which is what the venue's `variantData.startPrice` is.

| measure | definition |
| --- | --- |
| `travel` | distance walked inside the block ÷ σ_block |
| `net` | end-to-end displacement ÷ σ_block |
| `straightness` | \|net\| ÷ travel |
| `terminal` | \|close − strike\| ÷ σ_block |
| `crossings` | sign flips of (close − strike) across the block's samples |

Aggregated as medians over the recent window and over the baseline.

### Classification

Two axes, both as ratios to baseline:

| | low travel | high travel |
| --- | --- | --- |
| **low straightness** | FLAT | SWINGY |
| **high straightness** | (rare) | BIASED |

Starting thresholds, all ratios of the recent median to the baseline median
except `crossings` and `straightness`, which are absolute because they are
already dimensionless:

| state | condition |
| --- | --- |
| **FLAT** | `travel_ratio < 0.70` and `terminal_ratio < 0.70` |
| **BIASED** | `straightness >= 0.20` and `terminal_ratio >= 1.05` and `crossings <= 2.2` |
| **SWINGY** | `crossings >= 2.8` and `straightness < 0.14` and `travel_ratio >= 0.80` |

These are calibrated against **4032 real rounds** — fourteen days of 1s
BTCUSDT — and not chosen by intuition. Two earlier attempts were killed by
that data and both failures are worth recording, because they are the same
mistake at two removes.

The first draft used `0.55 / 0.40 / 1.2`, reasoned from the code. Once the
resolution measurement existed those numbers turned out to straddle the 1m
median, so a market of any character would have matched no rule at all.

The second draft used `0.35 / 0.12 / 3.0`, calibrated against the *per-round*
1s distribution. That was still wrong, and more subtly: **the classifier reads
medians over 24 rounds, not individual rounds**, and a median of 24 draws
concentrates far more tightly than the draws themselves. Per-round crossings
have p10 = 0, so `crossings <= 1.0` looked like a reasonable BIASED gate; the
24-round rolling mean has p5 = 1.00, so the same gate excluded 95% of the
fortnight and BIASED fired on **0.5%** of windows. The rule was effectively
dead and no amount of reading it would have shown that.

The shipped values come from a grid search over the rolling-window statistics
the classifier actually sees, constrained to produce zero overlap between the
three rules. What the market spent fourteen days doing, after hysteresis:

| state | share of rounds |
| --- | --- |
| FLAT | 32.3% |
| BIASED | 35.5% |
| SWINGY | 32.2% |
| UNKNOWN | 0.1% |

54 state changes in 13.6 days — one every 6 hours, median run 4.2 hours,
longest 31.3 hours. That is a market with three persistent states rather than
one that flickers, which is the claim this whole design rests on, now
measured rather than assumed.

When no rule matches, the committed state is held — an unclassifiable window
is not a fourth state, it is an absence of evidence to change.

These are starting values, tuned during the paper run. They are ratios so that
they survive a shift in the underlying volatility regime, which absolute
numbers would not.

### Known limitations

**One second is still a sample, not the tape.** A round that crosses the
strike twice inside one second is counted once. That is a far smaller error
than 1m's 3.3x undercount and it is the same estimator every round, so the
recent-against-baseline comparison stays valid either way.

**The detector now costs a request.** One per minute in steady state, eight at
startup. If that fetch fails the detector must hold its last committed reading
rather than fall back to 1m closes, because a reading taken at a different
resolution is not comparable to the baseline it would be scored against — it
would look like the market had abruptly become straighter and calmer, which is
exactly the false BIASED signal this whole change exists to prevent.

**Backtest and live must sample identically.** Thresholds calibrated on 1s
data are wrong on 1m data by the factors in the table above. The verification
harness and the live detector therefore read the same interval, and that is a
correctness requirement rather than a convenience.

### Hysteresis

A candidate state must hold for N consecutive readings before it is
committed. N defaults to **3** — one reading per round, so a regime change
commits after roughly 15 minutes of agreement, which is short enough to catch
a real shift inside a 1–2 hour state and long enough that a single odd round
cannot flip the strategy. The gap between "candidate differs from committed"
and "committed" is the `entering` slot, which is what produces the required
output:

```
MARKET exited FLAT 40m ago | currently BIASED | entering SWINGY (2/3)
```

`previous` with its age, `current` as the committed state, `entering` as the
uncommitted candidate with its progress shown. No forecast anywhere.

`RegimeReading` is a frozen dataclass carrying previous, age, current,
entering, candidate count, commit threshold, and the underlying measurements.
The measurements are journalled with the label so the classifier can be
audited against observation later.

## Component 2 — the playbook

Selecting the new `adaptive` profile turns the orchestrator on. Every other
profile still pins exactly as today, so `--profile buffer` is unchanged and
A/B comparison stays possible.

```python
PLAYBOOKS = {
    "SWINGY": [("straddle", {})],
    "BIASED": [("buffer",   {}),
               ("straddle", {"straddle_first_leg_max_price": 0.30})],
    "FLAT":   [("straddle", {}),
               ("pin",      {})],
}
```

Ordered list per regime; order is priority; each entry declines on its own
terms and the first that opens a position wins the round.

FLAT needs no new mechanism for its time split. `straddle` already self-gates
to the first 240s via `straddle_entry_window_s`, and `pin` gates itself to the
last 75s of a 300s round — that is, from 225s elapsed. The two therefore
overlap between 225s and 240s elapsed, and `straddle` wins that overlap by
being listed first. This is deliberate: inside the overlap a genuinely cheap
leg is the better trade, and `pin` picks up the round only once straddle's own
window has closed. Ordering plus each entry's own window is the same machinery
BIASED uses for fallback.

The effective config is re-resolved **only at round selection** — never
mid-round, never while a straddle leg is half open. Resolution is cached by
(regime, config version); building a frozen `Config` is cheap but there is no
reason to do it every poll.

### Two hazards

**Double entry.** `buffer` takes one side, `straddle` takes both, and
positions key on `(slug, Side)`. Guard: any open position for a slug ends
playbook iteration for that round.

**Orphaned scale-in.** A position opened by `buffer` must be managed under
`buffer`'s rules even after the regime changes. So a `Position` carries the
resolved profile that opened it, frozen for the position's life, and
`Journal.record` takes a per-trade profile rather than the constructor's
single `self._profile`. The column already exists, so the migration is safe,
and the change makes `--calibration-report` answer which regime's playbook
actually made money — the number that decides whether this project was worth
building.

## Component 3 — managing an open position

### Hedging is not free money

Measured, not assumed. A `buffer` leg staked 10.00 at 0.65, spot swung back so
the other side now costs 0.65, fee 200 bps:

| hedge | open leg wins | other wins | EV at market odds |
| --- | --- | --- | --- |
| 0.00 | +5.28 | −10.00 | −4.653 |
| 2.00 | +3.28 | −8.94 | −4.667 |
| 5.00 | +0.28 | −7.36 | −4.688 |

EV degrades as the hedge grows. At market prices hedging is EV-neutral before
fees and EV-negative after them. It does not recover a loss; it converts a
wide distribution into a narrow one and pays the venue for the conversion.

So a hedge is only worth placing for one of two reasons: it locks in money
that exists, or the quote is wrong about the adverse side.

### The decision table

```
open position, regime re-read each poll:
  lock-in available (guaranteed) ......... hedge, always
  SWINGY ................................ hold; no scale-in, no hedge
  BIASED, trend favours our side ........ scale in (buffer's existing rules)
  BIASED, trend against our side ........ hedge, unguaranteed, to cap
  FLAT .................................. hold
```

**Lock-in** is `straddle_completion_stake` (`btc_5m_predictor.py:3923`)
returning `guaranteed=True`, which happens exactly when
`be_open + be_other < 1` — the two fill prices, from their two different
moments, summing under one. That is arbitrage rather than a variance trade and
both branches pay. It requires the other side to have become *cheaper* than
complementary, which means spot ran further past the strike after entry.

**SWINGY holds.** SWINGY means spot crosses back. The venue prices the open
leg off current distance from strike; the regime says that distance
mean-reverts. Hedging there pays fees to cancel the signal that fired.

**BIASED-against hedges.** The trend continues, so the adverse side is likelier
than quoted and the hedge is EV-positive. This is the case where the position
is genuinely on the wrong side.

Scale-in survives only in BIASED-with-trend, which is the one regime where its
"press it while the market has inertia" premise was ever true.

No new machinery: `straddle_completion_stake` already returns a usable stake
when `guaranteed=False`, documented as part-hedging to cap a loss.

## Component 4 — `pin`, the FLAT strategy

### What it is

The first strategy that trades a conviction the pricing model cannot express.
In FLAT, spot sits near the strike, so `digital_up_probability` returns about
0.50 while the venue quotes 0.65 on the side spot is on. The model therefore
sees negative edge. `buffer` refuses because `min_buffer_sigmas: 0.75` needs a
distance FLAT does not have; `favorite` refuses because there is no edge. Both
are correct given what they know.

`pin` asserts something they do not model: the pricing model measures distance
from the strike, and the regime measures whether that distance moves at all.
In a flat hour it does not. `straddle` sets the precedent for bypassing
`clears_edge`; this bypass is written explicitly rather than achieved by
lowering a threshold until it stops binding.

### Gates

All must hold:

| gate | value | reason |
| --- | --- | --- |
| regime committed FLAT, `entering` empty | — | never fires mid-transition |
| `seconds_remaining <= pin_window_s` | 75.0 | the thesis is "it will not move in the time left" |
| `\|spot − strike\| >= pin_min_sigmas` | 0.15σ | at spot == strike a single tick flips the side |
| round's own `travel <= pin_max_round_travel` | 1.0σ | the regime describes 24 rounds; *this* round may be the breakout |
| `min_win_return` | 0.15 | caps the fill near 0.87 |

Side is whichever side spot is on. The model is not consulted.

### The risk, named

`pin` pays 0.85 to win 0.15, so one break erases roughly six wins. That is the
same asymmetry `buffer`'s comments were written to escape at 0.94. The
differences are that the ceiling is 0.85 rather than 0.95, and that the regime
gate and the round-travel gate are what earn the asymmetry. If FLAT detection
is wrong, this is the profile that pays for it, which is why the round-travel
guard is not optional.

```python
"pin": {"pin": True, "pin_window_s": 75.0, "pin_min_sigmas": 0.15,
        "pin_max_round_travel": 1.0,
        "min_entry_price": 0.50, "max_entry_price": 0.85,
        "min_win_return": 0.15, "max_stake_pct": 0.10,
        # Inert: the pin path never calls clears_edge. Left at the default
        # rather than 0.0 because Config validates min_edge in (0, 1) and a
        # zero would raise at startup. Same shape as straddle, which also
        # declares a min_edge its own path never consults.
        "min_edge": 0.02, "min_edge_ratio": 0.0,
        # Also inert -- pin does not scale in -- but Config requires it to
        # lie inside the entry band, so it is stated for this band rather
        # than inherited from a profile with a different one.
        "scale_in": False, "max_blended_price": 0.80}
```

Two settings above are inert and are declared anyway. That is not redundancy:
`Config.__post_init__` validates `min_edge` as strictly positive and requires
`max_blended_price` to sit inside `[min_entry_price, max_entry_price]`, so
omitting either would either raise at startup or silently inherit a value
belonging to a different price band.

## Changes by region

| where | change |
| --- | --- |
| new `RegimeReading` dataclass | previous + age, current, entering + (n/N), measurements, `describe()` |
| new `RegimeDetector` | per-round measures, baseline vs recent medians, hysteresis |
| `VolatilityEstimator` | cache a `RegimeReading` per symbol alongside `_trend`, from the same closes |
| `Config` | `pin`, `pin_window_s`, `pin_min_sigmas`, `pin_max_round_travel`, regime tuning fields |
| `PROFILES` | add `pin`, add `adaptive` |
| new `PLAYBOOKS`, `ADAPTIVE_ENVELOPE` | regime → ordered entries; envelope keys locked against overlays |
| `Position` | carries the resolved profile that opened it |
| `Journal.record` | per-trade profile argument (column already exists) |
| `Trader._maybe_enter` | iterate the playbook; first entry to open wins; one entry per slug |
| `Trader._maybe_scale_in` | apply the decision table |
| new `Trader._maybe_hedge` | route `straddle_completion_stake` at any open leg |
| new `Trader._maybe_enter_pin` | the `pin` entry path |

## Testing

All offline, `unittest`, matching the existing suite's conventions and its
`FakeClient` harness.

* **Classifier** — synthetic close series of known character (pure ramp,
  sawtooth across the strike, flat noise) assert the expected label.
* **Hysteresis** — a scripted reading sequence asserts commit timing and the
  `entering` counts.
* **Envelope** — an overlay touching a governance key raises `ValueError` at
  startup.
* **Playbook resolution** — governance fields are identical to base across all
  three regimes.
* **Position pinning** — flip the regime mid-round; scale-in must use the
  opening profile's rules.
* **Hedge table** — parametrized over (regime, `guaranteed`, trend alignment).
  Includes the EV case above as a regression: hedging an adverse leg in SWINGY
  must not fire.
* **`pin`** — fires only in committed FLAT with no transition and all four
  gates; refuses a breakout round via round-travel.
* **Simulated session** — `FakeClient` run through a scripted FLAT → BIASED →
  SWINGY sequence; assert the profile actually switched and that no round was
  double-entered.

## Verification plan

Paper mode against live data: `--profile adaptive`, `live: false`. The regime
line is logged every round with the measurements behind it, so the label can
be read against direct observation. `--calibration-report` splits by per-trade
profile and reports which regime's playbook made money.

## Constants to revisit after the paper run

Every constant below is specified and implementable as stated. These are the
ones chosen from reasoning rather than from data, listed so they are tuned
deliberately rather than discovered by surprise.

* `max_consecutive_losses: 10` in the envelope. The counter measures a bad
  straddle round and a full buffer stake under one name.
* The classification thresholds in Component 1.
* `pin_min_sigmas`, `pin_max_round_travel`, and `pin_window_s`, which together
  decide how often `pin` fires at all.
* Hysteresis N = 3.
