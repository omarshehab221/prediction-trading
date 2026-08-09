# BTC 5-minute prediction market bot

An automated trader for Binance Wallet Prediction Markets, restricted to the
BTC 5-minute Up/Down contract.

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
| `patterns.py` | 546 | Tests whether kline patterns predict anything |
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
| `--preflight` | Probe region, keys, wallet, balance, book, quote. |
| `--discover-min` | Measure the venue's real minimum order size. |
| `--whoami` | Show the outbound IP (for API key allowlists). |
| `--calibration-report` | Is the model calibrated? Per profile. |
| `--diagnose` | Is the edge real? Win rate vs break-even, per price band. |
| `--symbols A,B,C` | Markets to trade, each independently. |
| `--max-concurrent N` | How many markets may hold a position at once. |
| `--report-symbol` | Scope a report to one market. |
| `--live` / `--paper` | Pin the mode. Unset → the config file governs. |
| `--profile NAME` | Override the file's `active_profile`. |
| `--verbose` | Log the full signed request (signature redacted). |

Overrides: `--kelly`, `--min-edge`, `--fee-bps`, `--min-buffer`,
`--paper-bankroll`, `--scale-in` / `--no-scale-in`, `--report-every`,
`--no-fat-tails`, `--no-hot-reload`.

---

## 5. Markets

The bot trades any number of 5-minute up/down markets. Each is an independent
instrument:

```bash
python3 btc_5m_predictor.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --max-concurrent 3
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

Five strategies, differing in which contracts they buy:

| Profile | Entry band | Max stake | Buffer gate | Entry window | Paper |
|---|---|---|---|---|---|
| `buffer` *(default)* | 0.80–0.97 | 10% | ≥1.5σ | 180–15s | $25 |
| `favorite` | 0.55–0.80 | 10% | — | 120–20s | $25 |
| `micro` | 0.35–0.75 | 20% | — | 200–25s | $7 |
| `balanced` | 0.10–0.90 | 5% | — | 150–25s | $100 |
| `convex` | 0.05–0.35 | 2% | — | 280–30s | $100 |

**`buffer`** encodes "wait for a large buffer late in the round, then size up".
The gate is expressed in standard deviations of the *remaining* time, so the
same buffer counts for more as the clock runs down: ~22 bps with 150s left,
~10 bps with 30s.

**`convex`** buys longshots — the opposite side of the market. Only one of these
can be on the right side of any pricing bias, which is what the calibration
report's favourite-longshot table measures.

**`micro`** exists because a small account cannot use a percentage cap: at $6.64
a $1 minimum order *is* 15% of the balance. That risk is forced by arithmetic,
not chosen.

### Scale-in (buffer only)

Opens at 25% of the Kelly target, then tops up as the round moves further into
profit. This is **not** martingale: it adds only when the model's probability
has *risen*, and it targets the Kelly stake for the current probability rather
than stacking bets, so total exposure to one round stays Kelly-bounded. If the
round turns against you, nothing is added.

A counterintuitive constraint applies. Top-ups happen at a **higher** price, so
each one raises the blended fill and *shrinks* the payout — a large top-up can
turn a 6-wins-per-loss position into a 15-wins-per-loss one. `max_blended_price`
caps the blend; top-ups are trimmed to fit or skipped.

---

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

All 70 settings live in `config.json`. Layering: defaults → profile → file
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

## 10. Does the pattern idea work?

`patterns.py` tests 19 features — candlestick patterns, momentum, order flow —
against the next 5-minute outcome, with a chronological train/test split,
Benjamini-Hochberg correction, and a shuffled-label null run printed alongside
so you can see what chance looks like on your own data.

```bash
python3 patterns.py --selftest    # verify the harness first
python3 patterns.py --days 90
```

It passes its own self-test: finds **nothing** in pure noise, and recovers a
*planted* order-flow edge. A tool that cannot do both is worse than none.

Building it exposed two bugs in the harness itself. Overlapping forward windows
made a plain binomial test read a 49.1% hit rate on a **random walk** as
significant at p=0.023 (corrected: p=0.31). And `reversal_15m` was exactly
`NOT momentum_15m` — one coin flip counted as two tests.

The likely honest result on real data is that nothing survives. That is the
standard finding for candlestick patterns on short-horizon crypto.

---

## 11. Deployment

See `DEPLOY.md`. Three things will break it silently:

1. **Region must not be US.** Binance returns HTTP 451 to restricted locations.
   Render's `oregon`/`ohio`/`virginia` are all US.
2. **Config and journal need a persistent disk.** Ephemeral storage wipes the
   calibration record on every deploy — and that record is the whole point.
3. **`exec` in the entrypoint.** Without it the shell keeps PID 1, Python never
   receives SIGTERM, and every redeploy abandons a position mid-round.

Boot sequence: `verify.sh` → seed config (first boot only, never overwriting
your edits) → `--check-config` → `--preflight` → `exec` the bot.

---

## 12. What is verified, and what is not

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

## 13. Things that turned out to be wrong

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

## 14. Honest expectations

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
