# BTC 5-minute prediction market bot

An automated trader for Binance Wallet Prediction Markets. By default it
discovers and trades every 5-minute Up/Down contract the venue lists (BTC,
ETH, whatever else is listed); it can be restricted to specific markets if
you want fewer.

**Read this first:** the bot is built to find out whether an edge exists, not
to assume one. It spends most of its time doing nothing, and the most valuable
thing it produces is a calibration record telling you whether your strategy
beats break-even. A negative answer, cheaply obtained, is a real result.

---

## 1. How the market works

Each outcome is a **share priced between 0 and 1 that pays exactly 1 if it
wins**. That single fact drives everything:

- The price **is** the implied probability.
- Break-even win rate **equals the price** (plus fees).
- Buy at 0.95 → you win ~95% of the time, each win pays +5.3%, each loss costs
  −100%.
- Buy at 0.20 → each win pays +400%, and you win ~20% of the time.

So "win 95% of the time" and "small losses, big wins" cannot both be true at
fair prices. Choosing a win rate just picks a point on that curve. **The only
thing that makes money is buying a share for less than its true probability.**

### Where the edge is looked for

A 5-minute up/down contract is a digital option. With strike `S`, spot `P`,
time left `t` and volatility `σ`:

```
P(finishes up) = Φ( ln(P/S) / (σ√t) )
```

The bot computes this continuously and buys only when the order book asks
materially less. The numerator is the **buffer** (how far spot sits from the
strike); the denominator shrinks as the round runs out. Both are ways of making
the same bet more certain — which is exactly what a manual trader is estimating
when they say "big buffer, not much time left".

Two refinements matter:

- **Fat tails.** BTC returns are not Gaussian. A normal model materially
  understates the odds of the large move a cheap contract needs, so the bot
  fits a variance-matched Student-t whose degrees of freedom come from measured
  excess kurtosis. If the data does not look convincingly heavy-tailed, it stays
  Gaussian rather than inventing a parameter.
- **Volatility must be measured, not asserted.** If σ hits its floor or ceiling
  the bot **refuses to trade**, because an overstated σ inflates precisely the
  tail probabilities the strategy buys.

---

## 2. Files

| File | Lines | What it is |
|---|---|---|
| `btc_5m_predictor.py` | 4068 | The bot: client, pricing, risk, journal, CLI |
| `test_btc_5m.py` | 4694 | 512 tests across 78 classes |
| `conformance.py` | 403 | Validates every API call against Binance's own schema |
| `fuzz.py` | 363 | Property-based testing with hostile inputs |
| `coherence.py` | 376 | Finds stale artifacts, dead code, config drift |
| `mutate.py` | 155 | Mutation testing — measures test quality |
| `verify.sh` | 107 | The gate: runs before the bot is allowed to trade |
| `checkup.sh` | 139 | Full isolated verification run |
| `entrypoint.sh` | 118 | Deployment boot sequence |
| `Dockerfile`, `render.yaml` | — | Deployment manifests |
| `DEPLOY.md` | — | Hosting guide (read it before deploying) |

---

## 3. Quick start

```bash
pip install requests
export BINANCE_API_KEY=...  BINANCE_API_SECRET=...

python3 btc_5m_predictor.py --write-config      # create config.json
python3 btc_5m_predictor.py --preflight         # probe the live API
python3 btc_5m_predictor.py --discover-min      # find the real order minimum
python3 btc_5m_predictor.py                     # paper mode
```

After a few hundred paper rounds:

```bash
python3 btc_5m_predictor.py --calibration-report
python3 btc_5m_predictor.py --diagnose
```

Only then consider `--live`.

---

## 4. Commands

| Command | Purpose |
|---|---|
| `--write-config` | Emit every setting to a file. Refuses to overwrite. |
| `--check-config` | Validate the file. No network, no journal. |
| `--preflight` | Probe region, keys, wallet, balance, book, quote. Waits for IP allowlisting if `AUTH_WAIT_S` is set. |
| `--discover-min` | Measure the venue's real minimum order size. |
| `--whoami` | Show the outbound IP (for API key allowlists). |
| `--wait-for-auth N` | Seconds `--preflight` waits for a signed request to be accepted, so a shared outbound IP can be allowlisted while it retries. Same as `AUTH_WAIT_S`; the flag wins. |
| `--calibration-report` | Is the model calibrated? Per profile. |
| `--diagnose` | Is the edge real? Win rate vs break-even, per price band. |
| `--symbols A,B,C` | RESTRICT trading to these markets. Default: none, meaning every market is discovered and traded automatically. Same as the `SYMBOLS` env var; the flag wins if both are set. |
| `--max-concurrent N` | How many markets may hold a position at once. |
| `--report-symbol` | Scope a report to one market. |
| `--live` / `--paper` | Pin the mode. Unset → the config file governs. |
| `--profile NAME` | Override the file's `active_profile`. |
| `--verbose` | Log the full signed request (signature redacted). |

Overrides: `--kelly`, `--min-edge`, `--fee-bps`, `--min-buffer`,
`--min-return`, `--trend-follow` / `--no-trend-follow`, `--paper-bankroll`,
`--scale-in` / `--no-scale-in`, `--report-every`, `--no-fat-tails`,
`--no-hot-reload`.

---

## 5. Markets

By default the bot discovers and trades **every** 5-minute up/down market the
venue lists — no need to name them. Each traded market is an independent
instrument, capped by `max_concurrent_positions` (default 2) so an active
listing does not silently multiply exposure:

```bash
python3 btc_5m_predictor.py                          # every market, auto-discovered
python3 btc_5m_predictor.py --max-concurrent 3        # allow up to 3 concurrent positions
```

To restrict trading to specific markets instead, set `--symbols`, the
`SYMBOLS` environment variable (handy on a host where the command line is
fixed), or `symbols` in `config.json` — the flag wins if more than one is
set, and the config file is hot-reloadable so editing it does not require a
restart:

```bash
python3 btc_5m_predictor.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --max-concurrent 3
# or:  SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT python3 btc_5m_predictor.py --max-concurrent 3
```

**Isolated per market:** position slot, loss streak, calibration statistics,
volatility and tail estimates. A losing run on BTC does not gate ETH.

**Shared, because there is one account:** the bankroll, the daily loss limit,
the venue's daily quota.

That distinction forces a constraint. Sizing each market against the *full*
balance would let N markets quietly stack to N times the intended exposure — at
10% per position, three markets is 30% against a 25% hard cap. So new positions
size against **uncommitted** funds, and `reserve_pct` (30%) is held back
regardless of how many markets look attractive at once:

| Market | Available | Stake | Total committed |
|---|---|---|---|
| BTCUSDT | 70.00 | 7.00 | 7% |
| ETHUSDT | 63.00 | 6.30 | 13% |
| SOLUSDT | 56.70 | 5.67 | 19% |

Reports break down per market, and `--diagnose --report-symbol ETHUSDT` scopes
to one.

---

## 6. Profiles

Seven strategies, differing in which contracts they buy. The first five pick a
side from the model; the last two do not consult it at all.

| Profile | Entry band | Max stake | Buffer gate | Entry window | Paper |
|---|---|---|---|---|---|
| `buffer` | 0.55–0.80 | 10% | ≥0.75σ | 270–15s | $25 |
| `favorite` | 0.55–0.80 | 10% | — | 120–20s | $25 |
| `micro` | 0.35–0.75 | 20% | — | 200–25s | $7 |
| `balanced` | 0.10–0.90 | 5% | — | 150–25s | $100 |
| `convex` | 0.05–0.35 | 2% | — | 280–30s | $100 |
| `straddle` | both sides | 20% per leg | — | 240s from open | $100 |
| `lastminute` *(default)* | any | 10% per round | — | 60–5s | $100 |

**`buffer`** encodes "wait for a buffer to open, back the side it favours,
press it while the market has inertia — and refuse any price whose win is too
small to be worth the loss it risks". The gate is expressed in standard
deviations of the *remaining* time, so the same buffer counts for more as the
clock runs down: ~11 bps with 150s left, ~5 bps with 30s.

It is the only profile with a **return floor** (§6.1) and the only one that
**follows trends** (§6.2).

**`convex`** buys longshots — the opposite side of the market. Only one of these
can be on the right side of any pricing bias, which is what the calibration
report's favourite-longshot table measures.

**`micro`** exists because a small account cannot use a percentage cap: at $6.64
a $1 minimum order *is* 15% of the balance. That risk is forced by arithmetic,
not chosen.

**`straddle`** buys *both* sides of a round, at two different moments, and only
when the pair's worst case still pays back more than it cost. No side is ever
picked, so direction stops mattering.

**`lastminute`** is described in §6.3. It is the only profile that reads
nothing but the price.

### 6.1 The return floor (`buffer` only)

A positive edge says a bet is priced wrong. It says nothing about whether being
right pays enough to be worth the loss it risks — and those are different
questions, measured in different units.

Buying at 0.94 with a model probability of 0.99 clears every edge test
comfortably. It also returns about **6%** on a win, so **one loss erases
sixteen wins**. A day of patient, correct trading is undone by a single round
going the other way.

`min_win_return` sets a floor on the net profit per unit staked, after the
market's own fee. At `0.25` a win must pay at least a quarter of the stake,
which caps the fill price at `(1-fee)/((1-fee)+0.25)` — about **0.797** at a
2% fee. The ceiling is *derived from each market's published fee* rather than
written down, so a market with a different fee automatically gets a different
one. It is enforced everywhere money is committed: the screen, the walked book
average, the venue's executable quote, every top-up, and the blended price
across tranches.

**What this costs, stated plainly.** Price and buffer move together. A 1.5σ
buffer *is* a ~93% chance, and a market that has noticed will quote near 0.93 —
which the floor now refuses. So the trades that remain are the ones where the
buffer is real but **the book has not caught up**: the venue still quoting 0.75
while spot has already moved. That is a genuine edge, and there are fewer such
rounds than there were cheap-looking 0.94s. `min_buffer_sigmas` is 0.75 for the
same reason — demanding 1.5σ while capping the price at 0.80 asks for a 93%
chance at a 79% price, which almost never coexists.

**Not trading is not the same as not looking.** The entry window is 270–15s,
nearly the whole round, so the bot is watching when a good price appears rather
than sampling a narrow slice of it. A round declined for price is *not* written
off — it stays under review on every poll until it expires. And every expired
round is tallied by what blocked it:

```
No trade in 75 round(s) so far: 61 win pays less than the return floor;
  9 buffer too small for the time left; 5 edge below the floor
  The prices on offer were fine bets but small wins. Lower min_win_return to
  trade more of them, understanding that is the trade you asked not to make.
```

That line is the point. Silence is indistinguishable from a broken endpoint;
a reason is something you can act on.

### 6.2 Trend following (`buffer` only)

BTC often runs the same way for several rounds. While that lasts, entering
early is both cheaper and more likely to be right — which is where large
cumulative profits come from.

**The detector deliberately does not count consecutive rounds.** That detector
is structurally late: by the time three rounds have confirmed a move, the move
is three rounds old and the price is at its worst. Acting on a trend that has
already spent itself is close to a guaranteed loss.

So the trigger is the **current block**:

| Measure | Question it answers |
|---|---|
| `impulse` | How large is the move happening *right now*, in σ of one round-length? **This is the trigger.** |
| `run` | How many consecutive round-blocks agree? Corroboration and a brake — never the trigger. |
| `decay` | Current block's size relative to the previous one. Below `trend_decay_floor` the move is dying. |
| `rounds_left` | Projecting that decay: how many more rounds before it sinks under the noise floor? |
| `efficiency` | Net displacement ÷ total distance travelled. Catches a market swinging across the strike. |
| `z` | Net move over the run, in σ of the run. Confirms rather than announces. |

Direction comes from the block in progress, so a **reversal reads as a new
trend beginning at `run=1`**, not as the old trend continuing. A detector
anchored to the older blocks would still be reading UP at the moment price
turned down — the top of the move, and the single worst place to buy.

Phases: `none` → nothing happening · `building` → first thrust, tradable ·
`running` → confirmed and alive · `fading` → decaying, **refused**.

`trend_min_rounds_left` defaults to 1.0, which asks the only question an entry
actually poses: *does this move survive the round I am about to enter?*

When confirmed **and** pointing the same way as the trade, two things change:
the entry window widens by `trend_early_entry_s` (90s), and the stake is
multiplied by `trend_stake_multiple` (1.5×) — clipped by both the hard stake
cap and **2× full Kelly**, past which expected log growth is negative. The
widened window applies *only* to the side the trend favours; using a trend as
an excuse to trade against it is not a relaxation this grants.

Everything is measured from the same klines the volatility estimate already
fetches, so it costs no extra API calls, and `trend_z` is written to the
journal so you can later ask whether boosted trades earned their extra size.

---

### Scale-in (buffer only)

Opens at 25% of the Kelly target, then tops up as the round moves further into
profit. This is **not** martingale: it adds only when the model's probability
has *risen*, and it targets the Kelly stake for the current probability rather
than stacking bets, so total exposure to one round stays Kelly-bounded. If the
round turns against you, nothing is added.

A counterintuitive constraint applies. Top-ups happen at a **higher** price, so
each one raises the blended fill and *shrinks* the payout — a large top-up can
turn a 6-wins-per-loss position into a 15-wins-per-loss one. The effective cap
is `min(max_blended_price, whatever price still clears min_win_return at this
market's fee)`; top-ups are trimmed to fit or skipped. A position opened at
trend size tops up to the trend-sized target, so the boost is not silently
unwound halfway through the round.

---

### 6.3 The last minute (`lastminute`)

One rule, and nothing underneath it:

> With **60 seconds** left, buy whichever side is **dearer** — the one the book
> has already picked — provided it is quoted at **0.75 or better**. Inside the
> last **45 seconds**, buy it at whatever it costs. Stop at **5 seconds**.

No model probability, no edge test, no buffer, no trend, no volatility — none
of it is computed, let alone consulted. The premise is that a price is a
forecast, and in the last minute of a five-minute round it is a forecast with
almost no time left in which to be wrong.

The floor and the fallback are one rule in two halves, not a rule and an
excuse. A leader under 0.75 means the round is still a genuine contest and
there is time for it to stop being one, so nothing is bought yet. At 45s that
time has run out, and *"no side reached 0.75"* has itself become the answer:
the round **is** close, the leader is the best read available, and — because it
failed the floor — it is **cheap**.

**Where this can bleed is the first branch, not the fallback.** Nothing caps
the price above the floor, so a round already decided at 55s quotes 0.97 and
gets bought:

| Fill | Pays on a win | Wins to cover one loss |
|---|---|---|
| 0.60 (fallback) | +65% | 1.5 |
| 0.75 (floor) | +33% | 3.0 |
| 0.90 | +11% | 9.0 |
| 0.97 | +3% | 32.3 |

That is the profile as specified, and it is written down in the code rather
than guarded against. `--calibration-report`'s favourite-longshot table is what
says whether the venue's late favourites win often enough to pay for the dear
ones. Two rounds are left alone and neither is a price judgement: one where the
sides are quoted **level** (there is no dominant side, and picking one anyway
would invent the only signal this strategy refuses to have), and one where the
leader has rounded to **1.00** (it cannot pay back more than it cost).

The journal records the **market's** implied probability, not a forecast —
there is none. That is what lets the calibration breaker ask this profile's one
health question: *are the favourites I am buying winning as often as I paid for
them to?* It halts if they are not.

## 7. Risk controls

Every one fails closed.

- **Fractional Kelly** sizing (¼ Kelly by default), capped per profile.
- **Small-account override**: when Kelly sizes below the venue minimum, the
  minimum is used — but only while it stays under **2× full Kelly**, beyond
  which expected log growth is negative even with a genuine edge.
- **Daily loss limit**, tuned per profile. A shared 20% default halted `buffer`
  on 80% of days, because that profile risks 10% per trade.
- **Calibration halt**: stops when results are ≥2.5σ below what the model
  predicted. A raw streak counter is the wrong instrument for a low-win-rate
  strategy — at a 12% hit rate, 30-loss streaks are routine.
- **Clamped-σ refusal**, **price-impact cap**, **liquidity floor**,
  **consecutive-error kill switch**, **daily round cap**.
- **Graceful SIGTERM**: stops opening positions, lets the open one resolve.

---

## 8. Configuration and hot reload

All 99 settings live in `config.json`. Layering: defaults → profile → file
overrides → CLI.

The file is **re-read whenever it changes** — no restart. Reload is atomic and
fail-safe: the new config is fully built and validated before anything swaps, so
a malformed or invalid edit is rejected and logged while the bot keeps running
on its last good configuration.

| Edit | Result |
|---|---|
| Valid change | applied, diff logged |
| Invalid value | rejected, previous config kept |
| Malformed JSON | rejected, bot keeps running |
| Unknown setting | rejected by name |
| Deleted file | previous config kept |
| Immutable field | ignored, warned |

Immutable at runtime: API keys, `db_path`, `endpoints`, `profile_name`.

`live` is reloadable but **deferred** — a mode switch is applied only when the
bot is flat. Changing mid-round is incoherent in both directions: a paper
position has no real order behind it, and a real position flipped to paper stops
being tracked while its settlement is simulated.

---

## 9. Verification

Five independent tools, because the tests alone were not enough — every serious
bug in this project was found by running the thing, not by the suite.

| Tool | What it catches | Why it exists |
|---|---|---|
| `test_btc_5m.py` | Behaviour | 512 tests, including meta-tests over the source |
| `conformance.py` | Wrong API calls | Validates against Binance's own OpenAPI connector — **not** my model of the API |
| `fuzz.py` | Crashes, broken invariants | Random hostile input finds cases nobody would write |
| `coherence.py` | Stale artifacts | Dead code, unread config, drifted defaults |
| `mutate.py` | Weak tests | Injects bugs and reports which survive |

```bash
bash verify.sh                              # ~10s, the deploy gate
for i in 1 2 3; do bash checkup.sh $i; done # isolated, shuffled, full
python3 mutate.py --limit 70                # slow; development only
```

`checkup.sh` runs in a fresh directory with a fresh interpreter, a different
hash seed and **shuffled test order**, so it catches inter-test dependencies and
leaked state — not just "the suite passes twice".

Mutation testing is deliberately **not** a deploy gate: it takes 20+ minutes and
measures test *quality*, not correctness.

---

## 10. Deployment

See `DEPLOY.md`. Three things will break it silently:

1. **Region must not be US.** Binance returns HTTP 451 to restricted locations.
   Render's `oregon`/`ohio`/`virginia` are all US.
2. **Config and journal need a persistent disk.** Ephemeral storage wipes the
   calibration record on every deploy — and that record is the whole point.
3. **`exec` in the entrypoint.** Without it the shell keeps PID 1, Python never
   receives SIGTERM, and every redeploy abandons a position mid-round.

4. **Do not pin the mode in `entrypoint.sh`.** A `--live` flag on the exec line
   beats `TRADING_MODE`, so the manifest can read `TRADING_MODE=paper` while
   the process spends real money. `coherence.py` and the test suite both fail
   the build on this. Set `TRADING_MODE=live` instead.

Boot sequence: `verify.sh` → seed config (first boot only, never overwriting
your edits) → `--check-config` → `--preflight` → `exec` the bot.

### Shared outbound IP, and why preflight waits

Binance's API-key allowlist accepts individual addresses. A Render worker
without a dedicated IP does not know its outbound address until the process is
already running, and that address can change on any restart — so it cannot be
allowlisted in advance. Failing on the first refusal kills the deploy about a
second after printing the one piece of information needed to fix it.

`AUTH_WAIT_S` (default `600` in `render.yaml`) turns that race into a window.
Preflight prints the address, then knocks on a signed, IP-gated endpoint every
5 seconds until Binance accepts:

```
==> Outbound IP: 203.0.113.47
    This exact address must be on the API key's allowlist -- not the CIDR
    range a host's dashboard shows, which Binance cannot parse.
    Waiting up to 600s for a signed request to be accepted, retrying every
    5s. Add it now.

  attempt 1: [AUTH] Invalid API-key, IP, or permissions  (retrying, 595s left)
  attempt 2: [AUTH] Invalid API-key, IP, or permissions  (retrying, 590s left)
  accepted after 12s (3 attempts).
```

Only **auth refusals and network errors** retry. An HTTP 451 is the server's
*region*, not its address — no amount of waiting changes which continent the
worker is on — so it fails immediately with that explanation. Anything else is
a real fault and is reported at once rather than hidden behind ten minutes of
silence. The wait is bounded for the same reason: a paid worker idling forever
on a key that is simply wrong reports nothing, and Render restarts a failed
worker anyway.

`--whoami` samples the address repeatedly, which is how you find out whether it
is stable across requests at all.

---

## 11. What is verified, and what is not

**Verified by execution:** pricing model (vs scipy to machine precision), Kelly
sizing, edge math, order-book walking, payload parsing, risk limits, settlement,
config layering and hot reload, wei conversion, signing, deployment scripts.

**Verified against Binance's schema:** every request and response field, every
enum value, every wei-typed parameter.

**Not verified:** `place-order-bundle` against a funded account. Quotes are
non-binding, so everything up to execution is exercised by `--preflight`; the
order itself needs one real minimum-size trade.

**Unknowable from here:** whether the edge exists. No amount of testing answers
that. `--diagnose` does, from your own fills — and it needs roughly **525
resolved trades** to separate a 68% win rate from a 72% one. Those two look
identical over a few dozen trades, and one loses money while the other compounds.

---

## 12. Things that turned out to be wrong

Kept because they are the useful part of the history.

- **Signing.** Parameters were signed sorted, then sent in insertion order.
  Binance recomputes the HMAC over what it receives → `-1022` on every signed
  request.
- **Wei.** `amountIn` is 18-decimal wei. Sending `"5.00"` is off by 10¹⁸.
- **`feeRateBps or 200`.** A market publishing a genuine **zero** fee would have
  been silently overridden to 2%, because `0 or 200` is `200`.
- **`-3026`.** `fundingSource` was hardcoded `MPC` while `accountType` resolved
  to `SPOT` — those are CEX accounts. Contradictory, and the error named no field.
- **Balance 0.00.** `payment-options` covers CEX accounts only; a funded
  prediction wallet reads as empty there.
- **Kelly below the minimum.** On a small balance every profile returned $0.00
  and the bot would have idled forever, silently.
- **Winnings need claiming.** Redemption is an explicit on-chain action, so
  stakes left the balance while wins never returned — the risk manager read a
  *winning* streak as a drawdown and would have halted the day.
- **Duplicate keyword.** `main()` passed `kelly_fraction` explicitly while
  profiles set it too. Crashed on the first run with 384 tests green — because
  no test invoked `main()`.
- **Bankroll inflated by the gross payout.** Unclaimed winnings were added on
  top of a portfolio figure that already included settled positions. A $1 stake
  winning at 0.60 inflated the reported bankroll by **$1.67** (stake + profit)
  rather than $0.65. The API reading is now authoritative, and every live
  settlement is reconciled against the actual balance change.

The pattern in all of them: **treating a specific signal as a generic one.** A
numeric error code flattened to a string, a real balance replaced by a guessed
constant, a distinct failure reported as "something went wrong". The meta-tests
in `coherence.py` and `test_btc_5m.py` now enforce against that class directly.

---

## 13. Honest expectations

- The bot will often sit idle. With a 2% fee, a coin-flip contract loses ~2% per
  round by default, so declining to trade is correct behaviour.
- A high win rate is not profit. At price 0.95 you need 95% just to break even.
- Sizing cannot rescue a negative edge. Stake multiplies expected value; it
  cannot change its sign.
- Turning a small balance into a large one quickly requires bets that are far
  more likely to end at zero than the winning sessions suggest — those sessions
  and a wipeout have the same shape from the inside.

The realistic good outcome is not a fast return. It is knowing, from a few
hundred logged trades, whether the edge you believe in survives contact with
fees and fills.
