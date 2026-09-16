# The hybrid profile: straddle, buffer and a stop, layered per round

Design for one profile that takes a locked straddle when a round offers one,
a buffer entry when it does not, and puts a stop under every position that can
still lose.

Status: approved, not yet implemented.

## Why

Each of the three live strategies is good at one thing and poor at the rest:

| Profile    | Good at                                   | Poor at                                 |
|------------|-------------------------------------------|-----------------------------------------|
| `buffer`   | biased markets; high win rate             | smallest profit per round; a loss costs the whole stake |
| `straddle` | swingy markets; locked profit when both legs fill | a first leg that never completes rides as a directional bet |
| `scalp`    | bounding a loss with a watched stop       | barely profitable: the stop overshoots and eats the take-profits |

They are mutually exclusive today. `Trader._maybe_enter` dispatches to exactly
one of them (`btc5m/trader/core.py`), and `Config` refuses a BRACKET exit
without `scalp`. `hybrid` takes the strong part of each: straddle's lock,
buffer's selectivity, and scalp's stop, **without** scalp's take-profit, which
would cap buffer's 25%+ winners at 12%.

## The round, in order

Seconds are "remaining in the 5-minute round".

1. **300 -> 240 s: straddle opens.** `_maybe_enter_straddle` runs unchanged, and
   so does its gate: `straddle_require_positive_worst_case: True`,
   `straddle_force_hedge: False`. Completion of an open first leg continues
   past 240 s, as it does today, until `straddle_hedge_deadline_s`.
2. **240 -> 30 s: buffer enters.** `_maybe_enter_model` runs with buffer's gates
   unchanged (band 0.55-0.80, `min_win_return` 0.25, `min_buffer_sigmas` 0.75,
   trend-follow, scale-in), but **only on a market that holds no straddle leg**.
   The existing one-position-per-symbol rule already enforces that, because a
   straddle leg is a position on the symbol. Buffer's `entry_window_start_s` is
   set to 240 so the two windows cannot overlap.
3. **Always: the stop.** Every position that can still lose carries a watched
   stop:
   * a buffer position (including after scale-in top-ups);
   * a straddle first leg whose partner has not filled.

   A **completed straddle pair is never stopped**. Its outcome is already
   locked, and selling one leg would unlock it. When the second leg fills,
   the first leg's stop is removed on the same pass.

One market therefore holds either a straddle or a buffer position in a round,
never both.

## The stop

Stop-loss only. No take-profit, and no flatten. Winners ride to settlement.

* `hybrid_stop_loss_pct: 0.25`: fire when the bid is at or under
  `entry x (1 - 0.25)`. Scalp's 5% fired on noise 1-39 s after entry. At
  buffer's 0.55-0.80 entries a 5% stop would destroy the win rate the profile
  exists for. Stops sell into a falling bid and have overshot about 2x live
  (5% target, 11.7% realised), so expect a realised stop loss of roughly
  35-50% rather than the 100% of riding a loser.
* `hybrid_stop_disarm_s: 45`: inside the last 45 s no stop fires, and the
  position settles. The book is thinnest there, so a sale turns a coin flip
  into a certain loss.

Mechanics reuse scalp's: a `Bracket` in `self._brackets` with
`tp_price = inf` (never reached), checked by `_check_stops` on the stream bid,
sold through `_sell_now`. `_check_stops` currently returns early unless
`cfg.scalp`, so it will run for `cfg.hybrid` too, and it will skip a key whose
round is inside the disarm window. `_flatten_scalps` stays scalp-only.

The entry price the stop is measured from is the **executed** price
(`executed_price()` from the fill record), never the quote. A quote-priced stop
fired on a winning position on 2026-09-14. After a scale-in top-up the stop is
re-armed from the blended fill price.

### Delivered shares

A stop that cannot sell is worthless. Stops were refused with `-9000` on
2026-09-14 because the sale asked for the quote's share count rather than the
venue's. Scalp and straddle legs now record `filledShareQty`. **Buffer's model
entry does not**: it still sizes from cost. `model_entry.py` and
`scale_in.py` must record delivered shares the way `scalp.py` does, or every
hybrid buffer stop will be refused.

## Small balances: the $1 floor

**Requirement:** the profile must trade when 20% of the balance is under $1
(straddle legs, balance < $5) and when 10% is under $1 (buffer, balance < $10).
In both cases the stake is floored at `min_stake_usdt` (1.00).

Today every path refuses those rounds instead:

| Path | Current refusal | Hybrid behaviour |
|---|---|---|
| Straddle per-leg size (`straddle.py`, `per_side < min_stake_usdt` -> warn and return) | whole profile idles under $5 | `per_side = max(bankroll x 0.20, min_stake_usdt)` |
| Straddle both-now split (`_straddle_payouts_clear`, a leg under the minimum) | payout weighting can put the cheap leg under $1 even when the pair is funded | scale `total` up until the smaller leg is exactly `min_stake_usdt`; refuse only if `free` cannot cover that total; re-test the payout gate on the scaled stakes |
| Straddle first leg (`_open_first_leg`) | opens with only $1 free, then cannot afford the partner | open only when `free >= 2 x min_stake_usdt`, so a partner is always fundable |
| Straddle completion (`straddle_completion_stake` ideal < $1 -> skip) | a cheaper second leg sizes under $1 and never completes | `stake = max(ideal, min_stake_usdt)` capped at `free`, and `guaranteed` **re-derived for the floored stake** against the same floor/ceiling band. A floored stake above the ceiling is not a lock, so it keeps waiting as today |
| Buffer Kelly stake (`kelly_stake` round-up) | refused when $1 exceeds `hard_max_stake_pct` (0.25 here) or 2x full Kelly: at $3, $1 is 33% | for hybrid, any round with positive edge that passes every buffer gate stakes `max(kelly stake, min_stake_usdt)`, capped at `free`. The hard-cap and 2x-Kelly refusals are skipped **for hybrid only** |
| Buffer scale-in opener | already `max(stake x 0.25, min_stake_usdt)` | unchanged |
| Buffer scale-in top-up | needs >= $1 of room | unchanged; under $10 top-ups rarely fit, and the position stays at its $1 opener |

The skipped Kelly guard is a deliberate over-bet: at a $3 balance a $1 stake is
33% of bankroll, past the point where Kelly says long-run growth turns
negative. That is the trade-off being asked for. Each floored entry logs
it once per round: `stake floored to 1.00 (33% of 3.00; Kelly asked 0.21)`.

Lowest balances that can still trade, with `reserve_pct: 0.10`:

* buffer: `free >= 1.00` -> balance >= **1.12**
* straddle: `free >= 2.00` -> balance >= **2.23**

The existing halt at `bankroll < min_stake_usdt` stays.

`--preflight` / `probes.py` must report the floor for hybrid instead of saying
the profile cannot trade under $5 / $10.

## Settings

New `Config` fields:

* `hybrid: bool = False`
* `hybrid_stop_loss_pct: float = 0.25`: validated in (0, 1)
* `hybrid_stop_disarm_s: float = 45.0`: validated >= 0

Validation:

* `hybrid` excludes `scalp`, `straddle` and `last_minute`. Hybrid runs the
  straddle and model paths itself, so a set flag would dispatch twice.
* `hybrid` requires `exit_order_type: "NONE"`: the stop is its own mechanism,
  not a BRACKET exit, so the scalp <-> BRACKET rule is untouched.
* The straddle and model settings hybrid reads are validated as they are for
  their own profiles.

The `hybrid` profile entry in `profiles.py` carries buffer's gate settings,
straddle's `straddle_*` settings, and:

* `straddle_stake_pct: 0.20`, `max_stake_pct: 0.10`, `min_stake_usdt: 1.0`
* `entry_window_start_s: 240`, `entry_window_end_s: 30`,
  `straddle_entry_window_s: 60`
* `daily_loss_limit_pct: 0.50`: a floored $1 loss is a large share of a small
  bankroll; two losses at $3 still halt the day
* `max_concurrent_positions: 4`, `reserve_pct: 0.10`
* `exit_order_type: "NONE"`, `entry_order_type: "MARKET"`

The EFS `config.json` embeds `PROFILES`, so a deploy must regenerate it.

## Code

* `btc5m/config.py`: fields, validation.
* `btc5m/profiles.py`: `hybrid` entry, commented like its neighbours.
* `btc5m/trader/hybrid.py` (new): `HybridMixin._maybe_enter_hybrid` calls
  straddle entry, then model entry. `_sync_hybrid_stops` runs once per loop
  pass and makes the brackets match the positions: it arms any unpaired
  position, re-arms after a top-up, drops completed pairs, and never re-arms a
  stop that has already fired. That replaces separate arm/disarm calls at four
  entry points.
* Two existing behaviours would starve the buffer layer and are gated off for
  hybrid. First, the straddle path writes every round past its opening minute
  into `_seen`, which the model path skips. Second, a trend widens buffer's
  window past 240 s into the straddle minute, so the model path also refuses a
  round younger than `straddle_entry_window_s`.
* `max_consecutive_losses: 30`, not buffer's 6: every completed pair settles
  one leg as a loss.
* `btc5m/trader/core.py`: a `hybrid` branch first in `_maybe_enter`; mixin
  added.
* `btc5m/trader/scalp.py`: `_check_stops` runs for hybrid and honours the
  disarm window.
* `btc5m/trader/straddle.py`: floored per-leg size, scaled both-now split,
  `2 x min` first-leg check, floored completion stake. All gated on
  `cfg.hybrid`, so the `straddle` profile's behaviour does not change. Arm the
  stop on first-leg fill and disarm it on completion.
* `btc5m/sizing.py`: `kelly_stake` floors to the minimum when `cfg.hybrid`
  (it is reached through `assess`, so a flag on the config is the only seam
  that needs no signature change). `boosted_stake` never shrinks a hybrid
  stake below its input. `straddle_completion_band` is extracted, so a floored
  completion can be re-tested. Other profiles' paths are unchanged.
* `btc5m/trader/model_entry.py`, `btc5m/trader/scale_in.py`: record delivered
  shares; arm or re-arm the stop from the executed or blended price.
* `btc5m/probes.py`: hybrid preflight lines.
* `README.md`: profile entry.

## Testing

Test-first, in `tests/`:

* dispatch: a straddle leg on a market blocks a buffer entry on it; a market
  with no leg after 240 s is offered to buffer.
* stop: fires on a buffer position at `entry x 0.75`; fires on an unpaired
  first leg; never on a completed pair; removed when the second leg fills;
  silent inside the last 45 s; re-armed from the blended price after a top-up.
* small balance: at 4.00, a straddle leg stakes 1.00 (not 0.80); at 6.00, a
  buffer entry stakes 1.00 where Kelly asked less, including past the
  2x-Kelly and hard-cap limits; at 1.80 no straddle opens (free 1.62 < 2.00)
  but buffer can; a cheap completion floors to 1.00 and is only taken when
  the floored stake still locks the round; the `straddle` and `buffer`
  profiles are unchanged at those same balances.
* delivered shares: a buffer stop sells the venue's `filledShareQty`.
* config: hybrid with `scalp`/`straddle`/`last_minute`, or with a non-NONE
  exit, is rejected; stop settings out of range are rejected.

Per project practice the full suite is not run on Windows. Run the targeted
test files locally, then run `verify.sh` in WSL as the gate.

## Rollout

1. Commit to `master`.
2. WSL gate green.
3. **Shadow session first** (`TRADING_MODE=shadow`). Stops have not yet been
   proven to sell live across stop, partial fill and quote check, and this
   profile depends on them more than scalp did. Look for: stops firing and
   selling, the refused-sale count at zero, completed pairs never stopped,
   and floored $1 stakes at a small balance.
4. Live only after that session reads clean.

## Out of scope

* Scalp's momentum entries as a third layer.
* A take-profit on buffer positions.
* Fixing the stop's overshoot (resting the exit inside the spread). The
  25% width absorbs it rather than curing it.
* Regime detection. The layering picks per round by which gate clears first,
  not by classifying the market.
