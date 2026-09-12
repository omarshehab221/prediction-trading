# Single-Responsibility Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Break the 8,100-line `btc_5m_predictor.py` into a `btc5m` package where every
module has one responsibility, without changing what the bot does.

**Architecture:** Every definition moves **verbatim**, by line range, using a
purpose-built tool; nothing is retyped. `btc_5m_predictor.py` stays as the entry point
and re-exports the whole public surface, so `entrypoint.sh`, the tests, `fuzz.py` and
`checkup.sh` keep working unchanged. The two 1,000+ line classes (`PredictionClient`,
`Trader`) keep their identity and their `__init__`; their methods move into one mixin
per responsibility, so file-level single responsibility arrives without touching a
single call site or test fixture. A `check` command proves, after every task, that each
definition's AST is byte-for-byte the one recorded in the starting commit and that every
name still binds to the same object.

**Tech Stack:** Python 3.11 locally (3.12 in the image), stdlib only (`ast`,
`symtable`), unittest, bash gates (`verify.sh`, `entrypoint.sh`), Docker/Render.

**Spec:** this document — see *Design* below.

---

## Design

### Why

`btc_5m_predictor.py` holds the configuration schema, the profile table, the option
maths, sizing, the venue client, the journal, the entry gates, the paper simulator, the
trading loop, four strategies, the operator probes and the CLI. Any change to one of
them reads the other ten. The test file has the same problem at 11,000 lines.

### Target layout

```
btc_5m_predictor.py     entry point + public surface (re-exports only)
ws_feeds.py             unchanged, sockets
btc5m/
  constants.py          LOG, EPS, round length, fee fallback, EWMA + socket tolerances
  units.py              USDT <-> wei, lenient float parsing
  errors.py             error taxonomy: ErrorKind, ApiError and friends
  stats.py              normal and Student-t distribution functions
  pricing.py            what a contract is worth and what price clears
  sizing.py             how much to stake: Kelly, book walk, blend caps, straddle split
  pnl.py                what a settled trade paid
  domain.py             the value types: Side, Round, Signal, Position, PendingOrder...
  config.py             the Config dataclass and its validation
  profiles.py           PROFILES and DEFAULT_PROFILE
  config_file.py        the config document, hot reload, ConfigStore
  volatility.py         VolatilityEstimator and the trend measure
  risk.py               RiskManager: streaks, daily loss, calibration breaker
  journal.py            the sqlite record and its reports
  assessment.py         the entry gates and assess()
  paper.py              PaperBook, the paper order simulator
  probes.py             operator probes: preflight, whoami, discover-min
  cli.py                argument parsing and main()
  venue/
    endpoints.py        BASE, DEFAULT_ENDPOINTS, CEX_ACCOUNT_TYPES
    client.py           PredictionClient: construction, signing, transport, parsing
    spot.py             SpotApiMixin: Binance spot price and klines
    account.py          AccountApiMixin: wallets, balances, funding, quota
    markets.py          MarketsApiMixin: rounds, detail, order books
    orders.py           OrdersApiMixin: quote, place, confirm, cancel, order state
    settlement.py       SettlementApiMixin: settled outcome, redeem, final price
  trader/
    core.py             Trader: construction, the loop, mode switching, shutdown
    accounting.py       AccountingMixin: bankroll, exposure, per-market risk, resize
    claims.py           ClaimsMixin: the background redemption worker
    bookkeeping.py      BookkeepingMixin: pruning and missed-round tallies
    order_lifecycle.py  OrderLifecycleMixin: resting orders, fills, retraction
    exits.py            ExitsMixin: selling a position before settlement
    straddle.py         StraddleMixin
    model_entry.py      ModelEntryMixin
    last_minute.py      LastMinuteMixin
    scalp.py            ScalpMixin
    scale_in.py         ScaleInMixin
    settling.py         SettlementMixin: settlement and reconciliation
```

### Mixins, not collaborators

Tests reach into `Trader` privates (`t._maybe_enter_scalp`, `t._unredeemed`,
`t._brackets`) and `build_trader` rebuilds `Trader.__init__` by reflection. Extracting
collaborator objects would rewrite hundreds of tests, which is exactly the change that
cannot be proven safe. Mixins give one responsibility per file with zero call-site
churn, and they are the step that makes a later extraction local: each mixin file already
lists the state its responsibility touches. That later extraction is **out of scope
here** and deliberately so.

### How equivalence is proven

1. `split_tool.py move` copies source by line range — bodies are never retyped.
2. `split_tool.py check --baseline <rev>` re-parses the starting commit's monolith and
   asserts every definition's AST (ignoring line numbers) is identical to the one now in
   the package, that every baseline name exists exactly once, that the facade re-exports
   each one as *the same object*, and that no mixin member collides.
3. `symtable` proves no module references a global it does not import — an omitted
   import is a `NameError` on a branch tests may never take, so it is checked statically.
4. `tools/smoke.py` fingerprints the CLI (`--help`, `--write-config`, reports, refusals)
   and the pure numerics, and is compared against the fingerprint taken before any move.
5. The gates themselves (`coherence.py`, `conformance.py`, the source meta-tests,
   `verify.sh`) are taught about the package **before** any code moves, so they are
   watching during the split rather than after it.

---

## Global Constraints

- **Commit straight to master. No feature branches.** One commit per task.
- **Do not `git push` until Task 18.** `render.yaml` has `autoDeploy: true` and
  `TRADING_MODE: live`: every push restarts a live trading worker mid-round.
- **Never run the full suite locally, and never run `./verify.sh` locally.** The full
  run takes 10+ minutes on this Windows box and nests `verify.sh` inside itself. Run the
  classes each task names. `python coherence.py` and `python fuzz.py --trials 400` are
  seconds and are safe.
- **`TestDeploymentEntrypoint` and `TestVerificationGate` cannot pass on Windows** —
  Python cannot exec a `.sh` there. Never treat their local failure as a regression, and
  never "fix" them for Windows.
- **No behaviour changes.** The only intended behaviour change in this plan is Task 1,
  which is a bug fix, isolated in its own commit and separately revertible.
- **Moved code is never edited.** If a task seems to need an edit inside a moved body,
  stop and report instead.
- Python 3.11 is the local interpreter; the image runs 3.12. Tools must work on both.
- The logger name stays `"btc5m"`. The CLI is still `python btc_5m_predictor.py`.
- New `.py` files: UTF-8, LF in the repository (`* text=auto` already covers them).
- Commit messages follow the repo's voice: a sentence saying what changed and why, no
  `type:` prefix, ending with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## Task 1: The script-mode Side split (a live bug, fixed first)

`ws_feeds.py` resolves `Side` with a lazy `from btc_5m_predictor import Side`. Run as
`python btc_5m_predictor.py` — which is what `entrypoint.sh` does — the file is
`__main__`, so that import loads a **second copy** of the module with a **second `Side`
class**. `derive_asks` then compares `side is Side.UP` against an `UP` the trader built
from the first copy, the identity fails, and the UP ladder is derived from the DOWN side
of the book. Reproduced:

```
module Side.UP -> [(0.4, 5.0)]     # correct
script Side.UP -> [(0.7, 5.0)]     # what the running bot gets
same class? False | == works? True
```

`BookFeed.validate` compares REST against the derived ladder and rejects a market whose
mapping looks transposed, which is what has kept this from being visible: away from
0.50 the market is rejected and the bot silently stays on REST. Near 0.50 — where a
round starts — it can validate and then price UP off DOWN's ladder.

This lands first, alone, so it can be deployed or reverted on its own. **After it, the
WebSocket book will validate and be used far more often than it is today. That is the
designed behaviour, but it is a real change to how the live bot prices.**

**Files:**
- Modify: `btc_5m_predictor.py` (after the import block, around line 85)
- Test: `test_btc_5m.py` (new class at the end of the file)

**Interfaces:**
- Produces: no new names. `sys.modules["btc_5m_predictor"]` is aliased to the running
  module when the file is executed as a script.

- [ ] **Step 1: Write the failing test**

Append to `test_btc_5m.py`:

```python
class TestScriptModeSharesOneSide(unittest.TestCase):
    """
    One Side class, however the file was started.

    `python btc_5m_predictor.py` runs this file as __main__, so ws_feeds' lazy
    `from btc_5m_predictor import Side` used to import a SECOND copy of the
    module and get a SECOND Side class. `side is Side.UP` was then False for
    the UP the trader passed in, and the UP ladder came back derived from the
    DOWN side of the book -- the bot pricing UP off DOWN's prices.
    """

    def test_ws_feeds_sees_the_scripts_own_side(self):
        import subprocess, os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        code = (
            "import runpy, ws_feeds\n"
            "g = runpy.run_path('btc_5m_predictor.py', run_name='as_script')\n"
            "print(ws_feeds.derive_asks([(0.4, 5.0)], [(0.3, 5.0)], g['Side'].UP))\n"
        )
        env = dict(_os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
        r = subprocess.run([sys.executable, "-c", code], cwd=here, env=env,
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(r.stdout.strip(), "[(0.4, 5.0)]",
                         "ws_feeds derived the UP ladder from the DOWN side, "
                         "which means it resolved a different Side class")
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m unittest test_btc_5m.TestScriptModeSharesOneSide -v
```

Expected: FAIL, `'[(0.7, 5.0)]' != '[(0.4, 5.0)]'`.

- [ ] **Step 3: Alias the module to itself when it runs as a script**

In `btc_5m_predictor.py`, immediately after `import ws_feeds` and before
`LOG = logging.getLogger("btc5m")`:

```python
# Run as a script, this file is __main__ -- and ws_feeds' lazy
# `from btc_5m_predictor import Side` would then import it a SECOND time,
# producing a second Side class. `side is Side.UP` fails across those two
# classes, so the UP ladder came back derived from the DOWN side of the book.
# Registering the running module under its own name makes that import find
# this module instead of loading another copy of it.
if __name__ != "btc_5m_predictor":
    sys.modules.setdefault("btc_5m_predictor", sys.modules[__name__])
```

- [ ] **Step 4: Run the test and the socket tests**

```bash
python -m unittest test_btc_5m.TestScriptModeSharesOneSide test_btc_5m.TestBookFeed test_btc_5m.TestMarketData test_btc_5m.TestSpotFeed test_btc_5m.TestWsRecycle test_btc_5m.TestWsConfig -v
```

Expected: PASS, no errors.

- [ ] **Step 5: Check the gates still pass**

```bash
python coherence.py --source btc_5m_predictor.py --source ws_feeds.py
```

Expected: exit 0, `43 warning(s)`, no errors.

```bash
python fuzz.py --trials 400
```

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add btc_5m_predictor.py test_btc_5m.py && git commit -m "$(cat <<'EOF'
Give the bot one Side class however it was started

ws_feeds resolves Side with a lazy import of btc_5m_predictor. Started as a
script the file is __main__, so that import loaded a second copy of the module
and a second Side class; `side is Side.UP` was then False for the UP the
trader passed in, and derive_asks returned the UP ladder derived from the DOWN
side of the book. BookFeed.validate hid it: away from 0.50 the mapping looked
transposed and the market stayed on REST.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: The tool that moves the code and proves it did not change

Nothing in this plan retypes a line of the bot. This task builds the tool that copies
definitions by line range, generates each new module's imports from what the code
actually references, and proves afterwards that every definition is the one that started
in the monolith.

**Files:**
- Create: `tools/split_tool.py`
- Create: `tools/smoke.py`
- Create: `tools/baseline.txt` (the pre-split CLI fingerprint, deleted in Task 18)

**Interfaces:**
- Produces:
  - `python tools/split_tool.py move <module path>` — moves everything the map assigns
    to that module out of the facade and into it.
  - `python tools/split_tool.py check --baseline <rev>` — compares every definition
    against `<rev>:btc_5m_predictor.py`, checks for undefined globals and duplicate
    definitions, and asserts the facade re-exports the same objects. Exit 1 on any
    problem.
  - `python tools/split_tool.py leftovers` — comments the moves stranded in the facade.
  - `python tools/split_tool.py facade` — writes the final facade from the map.
  - `python tools/smoke.py` — a deterministic fingerprint of the CLI and the numerics.

- [ ] **Step 1: Write `tools/split_tool.py`**

```python
#!/usr/bin/env python3
"""
tools/split_tool.py -- move code into the btc5m package without changing it.

Scaffolding for one refactor; deleted when the refactor is done. Four jobs:

    move <module>        move every name the map assigns to <module>, verbatim
    check --baseline R   prove no definition and no binding changed since R
    leftovers            report comments the moves stranded in the facade
    facade               write the final re-export facade from the map

WHY A TOOL RATHER THAN AN EDITOR
--------------------------------
Eight thousand lines retyped by hand is eight thousand chances to turn a >=
into a >. Every byte that moves here is copied by line range, and `check`
compares the syntax tree of every definition against the commit the split
started from -- so "it still does the same thing" is a proven claim rather
than a careful one.

Imports are generated from what the code actually references, using symtable
rather than a name scan, so a local variable that happens to share a name with
a module-level one does not drag in an import. `check` then verifies the
opposite direction with symtable too: a module that references a global it
never imported is reported, because that is a NameError waiting on a branch
the tests may never take.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import os
import subprocess
import symtable
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACADE = os.path.join(ROOT, "btc_5m_predictor.py")

# Classes that keep their identity while their methods move into mixins.
HOSTS = ("PredictionClient", "Trader")

DUNDERS = {"__name__", "__file__", "__doc__", "__spec__", "__loader__",
           "__package__", "__builtins__", "__annotations__"}

THIRD_PARTY = {"requests", "websocket"}

PACKAGE_DOCS = {
    "btc5m": "The bot, one responsibility per module. btc_5m_predictor.py is "
             "the entry point and re-exports this package's public surface.",
    "btc5m/venue": "Everything that talks to the venue.",
    "btc5m/trader": "The trading loop and the strategies it runs.",
}

# module path -> (module docstring, [top-level names to move, in file order])
MODULES: dict[str, tuple[str, list[str]]] = {
    "btc5m/constants.py": (
        "Constants the whole bot shares: the logger, the float tolerance, the\n"
        "round length, the fee fallback and the two socket tolerances.",
        ["LOG", "DEFAULT_ROUND_SECONDS", "EPS", "DEFAULT_FEE_BPS",
         "BASIS_EWMA_ALPHA", "BASIS_EWMA_MIN_SAMPLES", "WS_BOOK_VALIDATE_TOL"]),
    "btc5m/units.py": (
        "How the venue writes numbers: 18-decimal wei, and strings that may or\n"
        "may not be a number at all.",
        ["WEI", "to_wei", "from_wei", "_as_float_or_none"]),
    "btc5m/venue/endpoints.py": (
        "The venue's addresses, verified against @binance/w3w-prediction.",
        ["BASE", "DEFAULT_ENDPOINTS", "CEX_ACCOUNT_TYPES"]),
    "btc5m/errors.py": (
        "What went wrong, classified by the venue's own code rather than by\n"
        "the wording of its message.",
        ["Shutdown", "TradingHalted", "ErrorKind", "ERROR_CODES",
         "_MESSAGE_HINTS", "_AboveBalance", "ApiError", "OrderNotFilled",
         "NothingToRedeem", "_ALREADY_REDEEMED_HINTS", "_is_already_redeemed"]),
    "btc5m/stats.py": (
        "Distribution functions: the normal CDF, and Student-t for fat tails.",
        ["norm_cdf", "_betacf", "betainc", "student_t_cdf",
         "standardised_t_cdf"]),
    "btc5m/pricing.py": (
        "What a 5m up/down contract is worth, and which prices clear a cost.",
        ["buffer_sigmas", "digital_up_probability", "breakeven_probability",
         "win_return", "max_price_for_return", "price_for_breakeven",
         "buy_reservation_price", "sell_reservation_price", "bracket_prices",
         "signal_edge_required"]),
    "btc5m/sizing.py": (
        "How much to stake: fractional Kelly, what the book can absorb, and\n"
        "the caps that keep a blended average honest.",
        ["kelly_stake", "kelly_multiple", "walk_book", "boosted_stake",
         "max_topup_within_blend", "straddle_split",
         "straddle_completion_stake"]),
    "btc5m/pnl.py": (
        "What a settled trade paid.",
        ["wins_per_loss", "settle_pnl", "straddle_worst_case_pnl"]),
    "btc5m/domain.py": (
        "The things the bot talks about: sides, orders, rounds, quotes,\n"
        "signals, positions and the state each of them carries.",
        ["Side", "Action", "OrderType", "OrderPlan", "_market_buy", "Round",
         "Quote", "OrderState", "Trend", "Signal", "Position", "PendingOrder",
         "Bracket", "WalletRef"]),
    "btc5m/config.py": (
        "Every setting the bot has, and the rules that make a set of them\n"
        "coherent enough to trade on.",
        ["Config"]),
    "btc5m/profiles.py": (
        "The strategies, as named sets of settings. DEFAULT_PROFILE is the one\n"
        "the deployment manifests seed on first boot.",
        ["PROFILES", "DEFAULT_PROFILE"]),
    "btc5m/config_file.py": (
        "The configuration as a file on disk: how it is written, how an edit\n"
        "to it reaches a running bot, and which fields refuse to change.",
        ["IMMUTABLE_FIELDS", "DEFERRED_FIELDS", "CONFIG_SCHEMA_NOTE",
         "default_config_document", "_coerce", "build_config", "ConfigStore"]),
    "btc5m/volatility.py": (
        "How volatile the market is, how fat its tails are, and which way it\n"
        "has been moving.",
        ["_projected_rounds", "VolatilityEstimator"]),
    "btc5m/risk.py": (
        "What stops the bot trading: losing streaks, the daily loss limit, and\n"
        "a model whose calibration has drifted.",
        ["RiskManager"]),
    "btc5m/journal.py": (
        "The record of what was traded and what it paid -- the only thing that\n"
        "can say whether the strategy has an edge.",
        ["Journal"]),
    "btc5m/assessment.py": (
        "Whether a round is worth entering, and at what size.",
        ["clears_edge", "clears_return", "blended_price_cap",
         "entry_window_start_s", "Assessment", "_DECLINE_ORDER", "_worse",
         "assess"]),
    "btc5m/paper.py": (
        "Paper orders, filled from the same book a live order would hit.",
        ["PaperBook"]),
    "btc5m/probes.py": (
        "Operator probes: what the bot checks before it is allowed to trade,\n"
        "and the two commands that answer 'who am I to the venue'.",
        ["IP_SERVICES", "outbound_ip", "wait_for_auth", "preflight", "whoami",
         "discover_min"]),
    "btc5m/cli.py": (
        "The command line: what each flag means and what it runs.",
        ["_parse_symbols_arg", "main"]),
    # The two host classes move last, after their mixins have been lifted out.
    "btc5m/venue/client.py": (
        "The venue client: construction, request signing, transport, and the\n"
        "parse-once rules for payloads that cannot be trusted.",
        ["PredictionClient"]),
    "btc5m/trader/core.py": (
        "The trading loop: what it owns, how it starts, how it switches mode\n"
        "and how it stops.",
        ["Trader"]),
}

# module path -> (mixin class, host class, module docstring, class docstring,
#                 [member names to move])
MIXINS: dict[str, tuple[str, str, str, str, list[str]]] = {
    "btc5m/venue/spot.py": (
        "SpotApiMixin", "PredictionClient",
        "Binance spot data. Model input only: the venue settles on its own\n"
        "feed, so nothing here decides an outcome.",
        "Spot price and klines, the model's view of the underlying.",
        ["market_symbol", "spot_price", "kline_closes"]),
    "btc5m/venue/account.py": (
        "AccountApiMixin", "PredictionClient",
        "Whose money, where it sits, and how much of it may be spent.",
        "Wallets, balances, funding source and the venue's quota.",
        ["wallet", "prediction_wallet_value", "payment_options",
         "funding_plan", "resolved_funding_source", "balance_usdt",
         "remaining_quota_usdt"]),
    "btc5m/venue/markets.py": (
        "MarketsApiMixin", "PredictionClient",
        "Which rounds exist and what their books look like.",
        "Round discovery, market detail and order books.",
        ["list_rounds", "market_detail", "hydrate", "asks_for", "bids_for"]),
    "btc5m/venue/orders.py": (
        "OrdersApiMixin", "PredictionClient",
        "Getting an order onto the book and finding out what happened to it.",
        "Quotes, orders, fills, cancellation and order state.",
        ["DEAD_ORDER_STATUSES", "FILLED_ORDER_STATUSES",
         "effective_slippage_bps", "_price_param", "get_quote",
         "discover_min_stake", "place_order", "order_fill", "confirm_fill",
         "active_orders", "cancel_orders", "order_state", "_read_order"]),
    "btc5m/venue/settlement.py": (
        "SettlementApiMixin", "PredictionClient",
        "How a round ended and how the winnings are claimed.",
        "Settled outcomes, redemption and the settlement price.",
        ["DEAD_REDEEM_STATUSES", "settled_outcome", "batch_redeem",
         "redeem_status", "final_price"]),
    "btc5m/trader/accounting.py": (
        "AccountingMixin", "Trader",
        "What the bot has, what it has committed, and what is left to stake.",
        "Bankroll, exposure, per-market risk and sizing against them.",
        ["_risk_for", "_committed", "_outstanding", "_available", "_bankroll",
         "_live_bankroll", "_resize"]),
    "btc5m/trader/claims.py": (
        "ClaimsMixin", "Trader",
        "Claiming a win, on a worker thread, so settling one round never makes\n"
        "the loop wait on a chain confirmation before it looks at the next.",
        "The background redemption worker.",
        ["_start_claim_worker", "_claim_worker_loop", "_forget_claim",
         "_claim_relentlessly", "_claim"]),
    "btc5m/trader/bookkeeping.py": (
        "BookkeepingMixin", "Trader",
        "Forgetting rounds that are over, and saying why the ones that passed\n"
        "were never entered.",
        "Pruning expired state and tallying missed rounds.",
        ["_prune", "_tally_missed"]),
    "btc5m/trader/order_lifecycle.py": (
        "OrderLifecycleMixin", "Trader",
        "A resting order from placement to fill, expiry or retraction. One\n"
        "seam, so paper and live share the whole lifecycle.",
        "Placing, reaping, retracting and booking resting orders.",
        ["_book_price", "_raw_book_price", "_place_leg", "_cancel_all_pending",
         "_reap_pending", "_retract", "_book_fill", "_post_limit_entry"]),
    "btc5m/trader/exits.py": (
        "ExitsMixin", "Trader",
        "Leaving a position before the oracle decides it.",
        "Exit pricing, exit orders and the sale they book.",
        ["_maybe_exit_all", "_model_prob", "_post_exit", "_book_sale",
         "_record_sale", "_sell_now"]),
    "btc5m/trader/straddle.py": (
        "StraddleMixin", "Trader",
        "Both sides of the same round, sized so the pair pays whichever way it\n"
        "resolves -- and the rules for a half-straddle waiting for its hedge.",
        "The straddle strategy.",
        ["_straddle_payouts_clear", "_completion_is_worth_waiting_out",
         "_open_first_leg", "_complete_half_straddles", "_record_straddle_legs",
         "_maybe_enter_straddle"]),
    "btc5m/trader/model_entry.py": (
        "ModelEntryMixin", "Trader",
        "Buying when the venue quotes materially less than the model's\n"
        "probability.",
        "The model-edge strategy.",
        ["_maybe_enter_model"]),
    "btc5m/trader/last_minute.py": (
        "LastMinuteMixin", "Trader",
        "Buying a favourite late, when the buffer is large in the time left.",
        "The last-minute strategy.",
        ["_maybe_enter_last_minute"]),
    "btc5m/trader/scalp.py": (
        "ScalpMixin", "Trader",
        "Trading the perp/spot dislocation: the futures lead, its bracket and\n"
        "the flattening that closes the round out.",
        "The futures-lead scalp strategy and its brackets.",
        ["_basis_dislocation_bps", "_scalp_signal", "_maybe_enter_scalp",
         "_arm_bracket", "_check_stops", "_flatten_scalps"]),
    "btc5m/trader/scale_in.py": (
        "ScaleInMixin", "Trader",
        "Adding to a position that is still cheap, without letting the blended\n"
        "average cross the price that clears the edge.",
        "Scaling into an open position.",
        ["_maybe_scale_in_all", "_maybe_scale_in"]),
    "btc5m/trader/settling.py": (
        "SettlementMixin", "Trader",
        "What the round paid, and whether the account agrees.",
        "Settlement and reconciliation against the venue.",
        ["_settle_open", "_settle_one", "_reconcile"]),
}

# The order moves must happen in: a module may only be created once every
# runtime name it references has a home.
ORDER = list(MODULES)


# --------------------------------------------------------------------------
# Reading the source
# --------------------------------------------------------------------------


def load(path: str) -> tuple[str, ast.Module]:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


def write(path: str, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def names_of(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def definitions(body: list[ast.stmt]) -> dict[str, list[ast.stmt]]:
    """name -> the statements that define it (a property and its setter: two)."""
    out: dict[str, list[ast.stmt]] = {}
    for node in body:
        for name in names_of(node):
            out.setdefault(name, []).append(node)
    return out


def block(lines: list[str], node: ast.stmt) -> tuple[int, int]:
    """
    The half-open line range of a definition, as 0-based indices.

    Starts at the first decorator and walks back over the comment lines
    directly above it, so a comment explaining the code travels with the code.
    A section banner is separated from what follows by a blank line, which is
    what stops this from dragging banners along.
    """
    start = min([node.lineno] + [d.lineno for d in
                                 getattr(node, "decorator_list", [])])
    i = start - 1
    while i > 0 and lines[i - 1].lstrip().startswith("#"):
        i -= 1
    return i, node.end_lineno


# --------------------------------------------------------------------------
# What a definition references
# --------------------------------------------------------------------------


def bound_here(expr: ast.AST) -> set[str]:
    """Names an expression binds itself -- comprehension targets, lambda args."""
    out: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.comprehension):
            out |= {n.id for n in ast.walk(node.target)
                    if isinstance(n, ast.Name)}
        elif isinstance(node, ast.Lambda):
            args = node.args
            out |= {a.arg for a in
                    args.posonlyargs + args.args + args.kwonlyargs}
    return out


def loaded(expr: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(expr)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            } - bound_here(expr)


def outer_names(node: ast.stmt) -> set[str]:
    """
    Names evaluated in the ENCLOSING scope rather than inside the definition:
    decorators, default arguments, base classes, and an assignment's value.
    symtable does not attribute these to the definition, so they are read off
    the tree instead.
    """
    parts: list[ast.AST] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        parts += node.decorator_list
        parts += [d for d in node.args.defaults if d is not None]
        parts += [d for d in node.args.kw_defaults if d is not None]
    elif isinstance(node, ast.ClassDef):
        parts += node.decorator_list + list(node.bases)
        parts += [k.value for k in node.keywords]
    elif isinstance(node, ast.Assign):
        parts.append(node.value)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        parts.append(node.value)
    out: set[str] = set()
    for part in parts:
        out |= loaded(part)
    return out


def annotation_names(node: ast.stmt) -> set[str]:
    """
    Names used only in annotations.

    `from __future__ import annotations` means these are never evaluated, so
    they need no runtime import -- but a reader and a type checker still want
    to see where they come from, which is what the TYPE_CHECKING block is for.
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        anns: list[ast.AST | None] = []
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = sub.args
            anns += [a.annotation for a in
                     args.posonlyargs + args.args + args.kwonlyargs]
            anns += [sub.returns,
                     args.vararg.annotation if args.vararg else None,
                     args.kwarg.annotation if args.kwarg else None]
        elif isinstance(sub, ast.AnnAssign):
            anns.append(sub.annotation)
        for ann in anns:
            if ann is not None:
                out |= {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)}
    return out


def scope_globals(table: symtable.SymbolTable) -> set[str]:
    """Every module-level name the scope and its nested scopes reference."""
    out: set[str] = set()
    if table.get_type() != "module":
        for sym in table.get_symbols():
            if sym.is_referenced() and sym.is_global():
                out.add(sym.get_name())
    for child in table.get_children():
        out |= scope_globals(child)
    return out


def tables_for(parent: symtable.SymbolTable, node: ast.stmt) -> list:
    """The symtable children covering one definition."""
    end = node.end_lineno or node.lineno
    return [t for t in parent.get_children()
            if node.lineno <= t.get_lineno() <= end]


def referenced(parent: symtable.SymbolTable, node: ast.stmt) -> set[str]:
    out = outer_names(node)
    for table in tables_for(parent, node):
        out |= scope_globals(table)
    if isinstance(node, ast.ClassDef):
        for table in tables_for(parent, node):
            for sym in table.get_symbols():
                if sym.is_referenced() and sym.is_global():
                    out.add(sym.get_name())
    return out


def undefined_globals(src: str, path: str) -> set[str]:
    """
    Names a module uses but never binds.

    This is the check that makes a generated import list safe: an import that
    was not generated is a NameError on whichever branch uses it, and that
    branch may be one no test takes.
    """
    table = symtable.symtable(src, path, "exec")
    bound = {s.get_name() for s in table.get_symbols()
             if s.is_assigned() or s.is_imported()}

    missing: set[str] = set()

    def visit(scope: symtable.SymbolTable) -> None:
        for sym in scope.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced():
                continue
            if scope.get_type() == "module":
                unresolved = not (sym.is_assigned() or sym.is_imported())
            else:
                unresolved = sym.is_global()
            if unresolved and name not in bound and name not in DUNDERS \
                    and not hasattr(builtins, name):
                missing.add(name)
        for child in scope.get_children():
            visit(child)

    visit(table)
    return missing


# --------------------------------------------------------------------------
# Writing a module
# --------------------------------------------------------------------------


def import_aliases(tree: ast.Module) -> dict[str, tuple]:
    """alias -> a description of the import statement that binds it."""
    out: dict[str, tuple] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out[a.asname or a.name.split(".")[0]] = ("import", a.name,
                                                         a.asname)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out[a.asname or a.name] = ("from", node.module, a.name,
                                           a.asname)
    return out


def module_name(path: str) -> str:
    return path[:-3].replace("/", ".").replace(os.sep, ".")


def home_of(name: str) -> str | None:
    for path, (_doc, names) in MODULES.items():
        if name in names:
            return module_name(path)
    return None


def fmt_from(mod: str, names: list[str]) -> str:
    line = f"from {mod} import {', '.join(names)}"
    if len(line) <= 79:
        return line
    body = ",\n    ".join(names)
    return f"from {mod} import (\n    {body},\n)"


def render_imports(aliases: dict[str, tuple], runtime: set[str],
                   annotated: set[str], tops: dict[str, list[ast.stmt]],
                   moving: set[str], where: str) -> list[str]:
    """
    The import block for a new module.

    Runtime names must already have a home: a module that reaches back into
    the facade would import the thing that is importing it.
    """
    plain: dict[str, list[str]] = {}
    typed: dict[str, list[str]] = {}

    def record(into: dict[str, list[str]], name: str) -> None:
        spec = aliases.get(name)
        if spec is not None:
            if spec[0] == "import":
                key = f"!import {spec[1]}" + (f" as {spec[2]}" if spec[2] else "")
                into.setdefault(key, [])
                return
            _kind, mod, orig, asname = spec
            into.setdefault(mod, []).append(
                orig if not asname else f"{orig} as {asname}")
            return
        mod = home_of(name)
        if mod is None:
            raise SystemExit(
                f"{where}: {name!r} has no home in MODULES and is not imported "
                f"by the facade")
        into.setdefault(mod, []).append(name)

    for name in sorted(runtime):
        if name in moving or name in DUNDERS or hasattr(builtins, name):
            continue
        if name in tops:
            raise SystemExit(
                f"{where}: {name!r} is still defined in the facade -- move it "
                f"first, or in this same batch")
        record(plain, name)
    for name in sorted(annotated):
        if name in moving or name in runtime or name in DUNDERS \
                or hasattr(builtins, name):
            continue
        record(typed, name)

    def group(mod: str) -> int:
        head = mod.removeprefix("!import ").split(" ")[0].split(".")[0]
        if head.startswith("btc5m") or head == "ws_feeds":
            return 2
        return 1 if head in THIRD_PARTY else 0

    lines = ["from __future__ import annotations"]
    for rank in (0, 1, 2):
        chunk = []
        for mod in sorted(plain):
            if group(mod) != rank:
                continue
            chunk.append(mod.removeprefix("!") if mod.startswith("!import ")
                         else fmt_from(mod, sorted(set(plain[mod]))))
        if chunk:
            lines += [""] + chunk
    if typed:
        lines += ["", "from typing import TYPE_CHECKING", "",
                  "if TYPE_CHECKING:"]
        for mod in sorted(typed):
            if mod.startswith("!import "):
                lines.append("    " + mod.removeprefix("!"))
            else:
                text = fmt_from(mod, sorted(set(typed[mod])))
                lines += ["    " + ln for ln in text.split("\n")]
    return lines


def docstring(text: str) -> list[str]:
    if "\n" in text:
        return ['"""'] + text.split("\n") + ['"""']
    return [f'"""{text}"""']


def ensure_packages(directory: str) -> None:
    rel = os.path.relpath(directory, ROOT).replace(os.sep, "/")
    parts = rel.split("/")
    for depth in range(1, len(parts) + 1):
        key = "/".join(parts[:depth])
        path = os.path.join(ROOT, *parts[:depth])
        os.makedirs(path, exist_ok=True)
        init = os.path.join(path, "__init__.py")
        if not os.path.exists(init):
            write(init, "\n".join(docstring(PACKAGE_DOCS[key])))


def insert_import(lines: list[str], mod: str, names: list[str]) -> None:
    """Put an import of the moved names at the end of the facade's header."""
    tree = ast.parse("\n".join(lines))
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, node.end_lineno)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        else:
            break
    lines.insert(last, fmt_from(mod, sorted(names)))


def cut(lines: list[str], spans: list[tuple[int, int]]) -> list[str]:
    out = list(lines)
    for i, end in sorted(spans, reverse=True):
        del out[i:end]
        while i < len(out) and out[i].strip() == "" \
                and (i == 0 or out[i - 1].strip() == ""):
            del out[i]
    return out


# --------------------------------------------------------------------------
# move
# --------------------------------------------------------------------------


def move(module: str) -> None:
    if module in MIXINS:
        move_members(module)
        return
    doc, names = MODULES[module]
    src, tree = load(FACADE)
    lines = src.split("\n")
    table = symtable.symtable(src, FACADE, "exec")
    tops = definitions(tree.body)
    aliases = import_aliases(tree)
    absent = [n for n in names if n not in tops]
    if absent:
        raise SystemExit(f"{module}: not in the facade: {absent}")

    moving = set(names)
    blocks: list[str] = []
    spans: list[tuple[int, int]] = []
    runtime: set[str] = set()
    annotated: set[str] = set()
    for name in names:
        for node in tops[name]:
            i, end = block(lines, node)
            blocks.append("\n".join(lines[i:end]).rstrip())
            spans.append((i, end))
            runtime |= referenced(table, node)
            annotated |= annotation_names(node)

    staying = {k: v for k, v in tops.items() if k not in moving}
    imports = render_imports(aliases, runtime, annotated, staying, moving,
                             module)
    path = os.path.join(ROOT, module)
    ensure_packages(os.path.dirname(path))
    write(path, "\n".join(docstring(doc) + [""] + imports + ["", ""])
          + "\n\n\n".join(blocks))
    write(FACADE, "\n".join(
        with_import(cut(lines, spans), module_name(module), names)))


def with_import(lines: list[str], mod: str, names: list[str]) -> list[str]:
    insert_import(lines, mod, names)
    return lines


def move_members(module: str) -> None:
    mixin, host, mod_doc, cls_doc, members = MIXINS[module]
    src, tree = load(FACADE)
    lines = src.split("\n")
    table = symtable.symtable(src, FACADE, "exec")
    tops = definitions(tree.body)
    aliases = import_aliases(tree)
    cls = tops[host][0]
    cls_table = next(t for t in tables_for(table, cls) if t.get_name() == host)
    body = definitions(cls.body)
    absent = [m for m in members if m not in body]
    if absent:
        raise SystemExit(f"{module}: not members of {host}: {absent}")

    blocks: list[str] = []
    spans: list[tuple[int, int]] = []
    runtime: set[str] = set()
    annotated: set[str] = set()
    for name in members:
        for node in body[name]:
            i, end = block(lines, node)
            blocks.append("\n".join(lines[i:end]).rstrip())
            spans.append((i, end))
            runtime |= outer_names(node)
            for child in tables_for(cls_table, node):
                runtime |= scope_globals(child)
            annotated |= annotation_names(node)
    runtime -= set(body)        # names that live on the class, not the module

    imports = render_imports(aliases, runtime, annotated, tops, set(), module)
    head = docstring(mod_doc) + [""] + imports + ["", "", f"class {mixin}:"]
    head += ["    " + ln if ln else "" for ln in docstring(cls_doc)] + [""]
    path = os.path.join(ROOT, module)
    ensure_packages(os.path.dirname(path))
    write(path, "\n".join(head) + "\n\n".join(blocks))

    out = cut(lines, spans)
    add_base(out, host, mixin)
    insert_import(out, module_name(module), [mixin])
    write(FACADE, "\n".join(out))


def add_base(lines: list[str], host: str, base: str) -> None:
    tree = ast.parse("\n".join(lines))
    cls = definitions(tree.body)[host][0]
    if cls.decorator_list or cls.keywords:
        raise SystemExit(f"{host} has decorators or keywords; rewrite by hand")
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)] + [base]
    header = f"class {host}({', '.join(bases)}):"
    if len(header) > 79:
        header = (f"class {host}(\n        " + ",\n        ".join(bases)
                  + "):")
    lines[cls.lineno - 1:cls.body[0].lineno - 1] = header.split("\n")


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------


def package_sources() -> list[str]:
    out = [FACADE]
    pkg = os.path.join(ROOT, "btc5m")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        out += [os.path.join(dirpath, f)
                for f in sorted(filenames) if f.endswith(".py")]
    return out


def dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def rel_of(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def family(host: str, current: dict) -> list[str]:
    """The host class and every base of it that lives in the package."""
    out = [host]
    for base in current[host][1][0].bases:
        if isinstance(base, ast.Name) and base.id in current:
            out += family(base.id, current)
    return out


def host_problems(host: str, base_node: ast.ClassDef, current: dict) -> list[str]:
    out: list[str] = []
    seen: dict[str, tuple[str, list[ast.stmt]]] = {}
    for name in family(host, current):
        where, nodes = current[name]
        for member, stmts in definitions(nodes[0].body).items():
            if member in seen:
                out.append(f"{host}.{member} is defined in both "
                           f"{seen[member][0]} and {where} -- the MRO now "
                           f"picks one of two")
            seen[member] = (where, stmts)
    base_members = definitions(base_node.body)
    for member, stmts in base_members.items():
        if member not in seen:
            out.append(f"{host}.{member} is gone")
        elif [dump(n) for n in stmts] != [dump(n) for n in seen[member][1]]:
            out.append(f"{host}.{member} is not the member it was "
                       f"({seen[member][0]})")
    for member in sorted(set(seen) - set(base_members)):
        out.append(f"{host}.{member} is new")
    if ast.get_docstring(base_node) != ast.get_docstring(current[host][1][0]):
        out.append(f"{host}'s docstring changed")
    return out


def binding_problems(base_tops: dict, current: dict) -> list[str]:
    """Every name must still resolve to ONE object, the facade's."""
    out: list[str] = []
    sys.path.insert(0, ROOT)
    facade_mod = importlib.import_module("btc_5m_predictor")
    for name in base_tops:
        if not hasattr(facade_mod, name):
            out.append(f"btc_5m_predictor no longer exports {name}")
            continue
        where = current[name][0]
        if where == "btc_5m_predictor.py":
            continue
        mod = importlib.import_module(module_name(where))
        if getattr(mod, name, None) is not getattr(facade_mod, name):
            out.append(f"{name} in {where} is not the object the facade "
                       f"exports")
    for path in package_sources():
        where = rel_of(path)
        if where == "btc_5m_predictor.py":
            continue
        mod = importlib.import_module(module_name(where))
        for name in base_tops:
            if name in vars(mod) \
                    and vars(mod)[name] is not getattr(facade_mod, name, None):
                out.append(f"{where} binds its own {name}, not the one the "
                           f"facade exports")
    return out


def check(baseline: str) -> int:
    shown = subprocess.run(
        ["git", "show", f"{baseline}:btc_5m_predictor.py"], cwd=ROOT,
        capture_output=True, text=True, check=True).stdout
    base_tops = definitions(ast.parse(shown).body)

    problems: list[str] = []
    current: dict[str, tuple[str, list[ast.stmt]]] = {}
    for path in package_sources():
        src, tree = load(path)
        where = rel_of(path)
        for name, nodes in definitions(tree.body).items():
            if name in current:
                problems.append(f"{name} is defined twice: "
                                f"{current[name][0]} and {where}")
            current[name] = (where, nodes)
        for name in sorted(undefined_globals(src, path)):
            problems.append(f"{where}: uses {name!r} without importing it")

    for name, nodes in base_tops.items():
        if name not in current:
            problems.append(f"{name} is gone")
            continue
        where, now = current[name]
        if name in HOSTS:
            problems += host_problems(name, nodes[0], current)
        elif [dump(n) for n in nodes] != [dump(n) for n in now]:
            problems.append(f"{name} is not the definition it was ({where})")

    problems += binding_problems(base_tops, current)

    for problem in problems:
        print(f"  {problem}")
    print(f"\n{len(base_tops)} definitions checked against {baseline}: "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# leftovers and the final facade
# --------------------------------------------------------------------------


def is_banner(text: str) -> bool:
    import re
    return bool(re.match(r"^# ?-{5,}$", text.strip()))


def leftovers() -> int:
    lines = load(FACADE)[0].split("\n")
    stranded = []
    for i, line in enumerate(lines, 1):
        text = line.strip()
        if not text.startswith("#") or is_banner(text):
            continue
        above = lines[i - 2].strip() if i >= 2 else ""
        below = lines[i].strip() if i < len(lines) else ""
        if is_banner(above) and is_banner(below):
            continue          # the title line of a section banner
        stranded.append(f"  L{i}: {text[:72]}")
    for row in stranded:
        print(row)
    print(f"\n{len(stranded)} comment line(s) left in the facade")
    return 0


def facade() -> None:
    src, tree = load(FACADE)
    lines = src.split("\n")
    doc_start, doc_end = block(lines, tree.body[0])
    alias = next(n for n in tree.body
                 if isinstance(n, ast.If) and "sys.modules" in ast.unparse(n))
    alias_start, alias_end = block(lines, alias)

    parts = lines[doc_start:doc_end] + [
        "",
        "from __future__ import annotations",
        "",
        "import sys",
        "",
    ] + lines[alias_start:alias_end] + [""]
    for path, (_doc, names) in MODULES.items():
        parts.append(fmt_from(module_name(path), sorted(names)))
    parts += ["", "", 'if __name__ == "__main__":',
              "    raise SystemExit(main())"]
    write(FACADE, "\n".join(parts))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mover = sub.add_parser("move")
    mover.add_argument("module", choices=list(MODULES) + list(MIXINS))
    checker = sub.add_parser("check")
    checker.add_argument("--baseline", required=True)
    sub.add_parser("leftovers")
    sub.add_parser("facade")
    args = ap.parse_args(argv)

    if args.cmd == "move":
        move(args.module)
        print(f"moved {args.module}")
        return 0
    if args.cmd == "check":
        return check(args.baseline)
    if args.cmd == "leftovers":
        return leftovers()
    facade()
    print("facade rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Prove the tool moves code without changing it, on a scratch copy**

```bash
python - <<'PY'
import ast, os, shutil, subprocess, sys, tempfile
root = os.getcwd()
tmp = tempfile.mkdtemp()
for name in ("btc_5m_predictor.py", "ws_feeds.py"):
    shutil.copy(name, tmp)
os.makedirs(os.path.join(tmp, "tools"))
shutil.copy("tools/split_tool.py", os.path.join(tmp, "tools"))
r = subprocess.run([sys.executable, "tools/split_tool.py", "move",
                    "btc5m/constants.py"], cwd=tmp, capture_output=True,
                   text=True)
print(r.stdout, r.stderr)
before = ast.parse(open(os.path.join(root, "btc_5m_predictor.py"),
                        encoding="utf-8").read())
after = ast.parse(open(os.path.join(tmp, "btc5m", "constants.py"),
                       encoding="utf-8").read())
was = {n.target.id if hasattr(n, "target") else n.targets[0].id: ast.dump(n)
       for n in before.body if isinstance(n, (ast.Assign, ast.AnnAssign))
       and getattr(n, "targets", [getattr(n, "target", None)])[0] is not None}
now = {n.targets[0].id: ast.dump(n) for n in after.body
       if isinstance(n, ast.Assign)}
print("identical:", all(was[k] == now[k] for k in now))
print(open(os.path.join(tmp, "btc5m", "constants.py"), encoding="utf-8").read()[:400])
shutil.rmtree(tmp, ignore_errors=True)
PY
```

Expected: `identical: True`, and the printed head of `btc5m/constants.py` shows the
docstring, `from __future__ import annotations`, `import logging`, then `LOG = ...`
with its original comments. The real repository is untouched — only the scratch copy was
moved.

- [ ] **Step 3: Write `tools/smoke.py`**

```python
#!/usr/bin/env python3
"""
tools/smoke.py -- a fingerprint of what the bot does, for the package split.

Runs the CLI surface and the pure numerics and prints one text block. Taken
before the split and after it, the two must be identical: the tests prove the
logic, this proves the thing a user actually invokes still answers the same.

Scaffolding for one refactor; deleted with it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT = os.path.join(ROOT, "btc_5m_predictor.py")
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ", re.M)

CASES = [
    ("help", ["--help"], {}),
    ("default-profile", ["--print-default-profile"], {}),
    ("write-config", ["--config", "cfg.json", "--db", "j.db",
                      "--profile", "convex", "--write-config"], {}),
    ("check-config", ["--config", "cfg.json", "--db", "j.db",
                      "--check-config"], {}),
    ("reject-bad-kelly", ["--kelly", "9"], {}),
    ("no-keys", [], {"BINANCE_API_KEY": ""}),
    ("calibration-convex", ["--profile", "convex", "--calibration-report",
                            "--db", "cal_convex.db"], {}),
    ("calibration-balanced", ["--profile", "balanced", "--calibration-report",
                              "--db", "cal_balanced.db"], {}),
]


def numerics() -> str:
    sys.path.insert(0, ROOT)
    import btc_5m_predictor as m
    cfg = m.Config(api_key="k", api_secret="s", **m.PROFILES["convex"])
    values = []
    for df in (None, 2.5, 4.0, 12.0):
        for spot in (99_000, 99_800, 100_000, 100_400, 101_500):
            values.append(m.digital_up_probability(spot, 100_000, 0.55, 90, df))
    for price in (0.05, 0.12, 0.2, 0.35, 0.6, 0.9):
        values.append(m.breakeven_probability(price, 200))
        values.append(m.kelly_stake(1000, min(price * 1.5, 0.99), price, cfg))
        values.append(m.settle_pnl(10, price, True, 200))
        values.append(m.win_return(price, 200))
    digest = hashlib.sha256(
        ";".join(f"{v:.15e}" for v in values).encode()).hexdigest()
    return f"## numerics\n{digest}"


def main() -> int:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        env_base = dict(os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
                        PYTHONHASHSEED="0")
        for key in ("TRADING_MODE", "SYMBOLS", "PROFILE", "CONFIG_PATH",
                    "DB_PATH"):
            env_base.pop(key, None)
        for label, args, extra in CASES:
            proc = subprocess.run([sys.executable, BOT, *args], cwd=tmp,
                                  env={**env_base, **extra},
                                  capture_output=True, text=True, timeout=180)

            def clean(text: str) -> str:
                text = text.replace(tmp, "<TMP>")
                text = text.replace(tmp.replace("\\", "/"), "<TMP>")
                return STAMP.sub("<TS> ", text)

            out.append(f"## {label} exit={proc.returncode}\n"
                       f"{clean(proc.stdout)}\n-- stderr --\n"
                       f"{clean(proc.stderr)}")
        with open(os.path.join(tmp, "cfg.json"), encoding="utf-8") as fh:
            out.append("## cfg.json\n" + fh.read())
    out.append(numerics())
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Record the baseline, and check it is deterministic**

```bash
python tools/smoke.py > tools/baseline.txt && python tools/smoke.py > tools/second.txt && python - <<'PY'
a = open("tools/baseline.txt", encoding="utf-8").read()
b = open("tools/second.txt", encoding="utf-8").read()
print("deterministic:", a == b)
PY
```

Expected: `deterministic: True`. **If it is False, stop and report which section
differs** — something in the CLI output carries a clock or a path, and the fingerprint
needs to normalise it before it is any use as a baseline.

```bash
rm tools/second.txt && git rev-parse HEAD
```

Record that revision as `<BASELINE>`; every later `check` is run against it. Write it
into the first line of `tools/baseline.txt` as a comment:

```bash
python - <<'PY'
import subprocess
rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                     text=True).stdout.strip()
path = "tools/baseline.txt"
text = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8", newline="\n").write(
    f"# baseline revision: {rev}\n" + text)
print(rev)
PY
```

- [ ] **Step 5: Check the tool agrees the untouched monolith matches its own baseline**

```bash
python tools/split_tool.py check --baseline HEAD
```

Expected: `0 problem(s)`, and a count of roughly 100 definitions. This is the control
run: the package does not exist yet, so every definition is found in the facade itself.

Then check the map accounts for every name, because a name the map forgets is a name
that never leaves the facade and is never noticed:

```bash
python - <<'PY'
import ast, sys
sys.path.insert(0, "tools")
import split_tool as st
tree = ast.parse(open("btc_5m_predictor.py", encoding="utf-8").read())
baseline = {n for node in tree.body for n in st.names_of(node)}
mapped = {n for _doc, names in st.MODULES.values() for n in names}
host_members = {m for _c, _h, _d, _cd, members in st.MIXINS.values()
                for m in members}
print("unmapped:", sorted(baseline - mapped))
print("mapped but not in the bot:", sorted(mapped - baseline))
print("mixin members:", len(host_members))
PY
```

Expected: both lists empty, and **80 mixin members** — 33 of `PredictionClient`'s 52
and 47 of `Trader`'s 59. The 19 members `PredictionClient` keeps are its class
attributes, `__init__`, `_cfg`, `apply_config`, `session`, `sync_clock`, `now_ms`,
`_signed_query`, `_ERROR_HINTS`, `_json_or_none`, `_request` and the four parsers; the 11
`Trader` keeps are `__init__`, `_resolve`, `_cfg`, `_orders`, `_position`, `_live`,
`_apply_pending_mode`, `_install_signal_handlers`, `run`, `_maybe_enter` and `_drain`.
If those two lists differ, the map and the code have drifted — stop.

- [ ] **Step 6: Commit**

```bash
git add tools && git commit -m "$(cat <<'EOF'
Build the tool that will move the bot without rewriting it

The split moves eight thousand lines. Retyped by hand that is eight thousand
chances to turn a >= into a >, so nothing is retyped: split_tool moves code by
line range and then compares the syntax tree of every definition against the
commit the split started from. smoke.py records what the CLI answers today, so
the same questions can be asked after every move.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Teach coherence.py that the bot is more than one file

`coherence.py` runs four checks against `sources[0]` only: the Config audit, the CLI
audit, the default-profile audit and the mode-flag audit. Once `btc_5m_predictor.py` is a
facade, three of them would find nothing and say so silently — a gate that checks an
empty room. This task moves them onto the merged corpus, teaches coherence which files
the bot is made of, and stops mixin classes producing a warning for every attribute
their host assigns.

**Files:**
- Modify: `coherence.py`
- Test: `test_btc_5m.py` (`TestCoherenceCorpus`)

**Interfaces:**
- Produces: `coherence.package_sources(root)` (facade + `btc5m/**/*.py`),
  `coherence.bot_sources(root)` (that plus `ws_feeds.py`),
  `coherence.analyse(sources) -> Findings`, `coherence.class_families(tree)`.
  Running `python coherence.py` with no `--source` now analyses the whole bot.

- [ ] **Step 1: Write the failing tests**

Add to `TestCoherenceCorpus` in `test_btc_5m.py`:

```python
    def test_default_sources_are_the_whole_bot(self):
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        sources = self.coherence.bot_sources(here)
        self.assertTrue(sources[0].endswith("btc_5m_predictor.py"),
                        f"the facade must lead, got {sources[:1]}")
        self.assertTrue(any(s.endswith("ws_feeds.py") for s in sources))
        for path in sources:
            self.assertTrue(_os.path.exists(path), path)

    def test_the_config_checks_read_every_file(self):
        """
        Config, the CLI and the profile table may live in any file.

        These four checks used to read only the first source. That was right
        while the first source was the whole bot; with the bot in a package it
        would check a facade that declares none of them and report nothing --
        a gate that passes because it looked in an empty room.
        """
        facade = self._write("import sys\n")
        rest = self._write(
            'PROFILES: dict[str, dict] = {\n'
            '    "p": dict(min_edge=0.1),\n'
            '}\n'
            'DEFAULT_PROFILE = "p"\n'
            'class Config:\n'
            '    min_edge: float = 0.05\n'
            '    unread_setting: float = 1.0\n')
        f = self.coherence.analyse([facade, rest])
        self.assertEqual(
            [e for e in f.errors if "no Config class" in e], [],
            f"the Config audit did not find Config in the corpus: {f.errors}")
        self.assertTrue(
            any("unread_setting" in e for e in f.errors),
            f"a dead setting in the second file went unreported: {f.errors}")

    def test_a_mixin_may_read_what_its_host_assigns(self):
        """
        A mixin's methods run on the host's instance.

        Splitting a class across files must not make every attribute the host
        assigns read as one the mixin never sets -- that is 200 warnings for
        code that is working, which is how a report stops being read.
        """
        mixin = self._write("class ScalpMixin:\n"
                            "    def enter(self):\n"
                            "        return self._client\n")
        host = self._write("class Trader(ScalpMixin):\n"
                           "    def __init__(self):\n"
                           "        self._client = 1\n")
        f = self.coherence.analyse([host, mixin])
        self.assertEqual(
            [w for w in f.warnings if "_client" in w], [],
            f"_client is assigned by the host: {f.warnings}")

    def test_a_mixin_may_call_a_sibling_mixins_method(self):
        one = self._write("class OneMixin:\n"
                          "    def enter(self):\n"
                          "        return self.settle()\n")
        two = self._write("class TwoMixin:\n"
                          "    def settle(self):\n"
                          "        return 1\n")
        host = self._write("class Trader(OneMixin, TwoMixin):\n"
                           "    pass\n")
        f = self.coherence.analyse([host, one, two])
        self.assertEqual(
            [w for w in f.warnings if "settle" in w], [],
            f"settle() is a sibling mixin's method: {f.warnings}")
```

Replace the body of `test_the_real_project_is_still_coherent` with the default-source
form, so the list of the bot's files lives in exactly one place:

```python
    def test_the_real_project_is_still_coherent(self):
        """The change must not make the actual codebase report new findings."""
        import subprocess as _sp
        import sys as _sys
        import os as _os
        here = _os.path.dirname(_os.path.abspath(__file__))
        proc = _sp.run([_sys.executable, "coherence.py"], cwd=here,
                       capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
python -m unittest test_btc_5m.TestCoherenceCorpus -v
```

Expected: 4 failures — `bot_sources`, `analyse` and `class_families` do not exist yet
(`AttributeError`).

- [ ] **Step 3: Add the source lists and `analyse()` to `coherence.py`**

After the `CORPUS_*` globals, add:

```python
ROOT = os.path.dirname(os.path.abspath(__file__))


def package_sources(root: str = ROOT) -> list[str]:
    """
    Every file the bot itself is made of, the facade first.

    The facade leads because the checks that read the deployment manifests
    resolve them next to the first source, and the facade sits at the root.
    """
    out = [os.path.join(root, "btc_5m_predictor.py")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "btc5m")):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        out += [os.path.join(dirpath, name)
                for name in sorted(filenames) if name.endswith(".py")]
    return [path for path in out if os.path.exists(path)]


def bot_sources(root: str = ROOT) -> list[str]:
    """The bot, plus the transport module it reads its feeds through."""
    out = package_sources(root)
    feeds = os.path.join(root, "ws_feeds.py")
    if os.path.exists(feeds):
        out.append(feeds)
    return out
```

- [ ] **Step 4: Move the whole-bot checks onto the corpus**

Replace the body of `main()` from `sources = ...` down to the `print(...)` with a call to
a new `analyse()`, defined just above `main()`:

```python
def analyse(sources: list[str]) -> Findings:
    """Run every check over a corpus and return what it found."""
    global SOURCE_PATH
    loaded = set_corpus(sources)
    f = Findings()

    for path, src, tree in loaded:
        SOURCE_PATH = path
        for check in (check_dead_functions, check_attributes,
                      check_stale_prose, check_magic_numbers):
            check(src, tree, f)

    # These four describe the bot as a whole -- its one Config, its one
    # argument parser, its one profile table, its one default profile -- so
    # they read the merged corpus. They used to read the first file only,
    # which was the same thing while that file was the whole bot. With the bot
    # in a package it would mean checking a facade that declares none of them
    # and reporting nothing, which is worse than not checking at all.
    SOURCE_PATH = sources[0]
    for check in (check_config, check_cli, check_default_profile,
                  check_mode_flags):
        check(CORPUS_SRC, CORPUS_TREE, f)
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", action="append", default=None,
                    help="analyse this file; repeat for a multi-file corpus")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args()

    sources = args.source or bot_sources()
    f = analyse(sources)

    print(f"=== COHERENCE: {', '.join(os.path.basename(s) for s in sources)} "
          f"===\n")
```

Leave the rest of `main()` (the printing and the exit code) exactly as it is.

- [ ] **Step 5: Anchor the PROFILES lookup**

In `check_config`, the profile block is now located in a corpus that may mention
`PROFILES` in prose before it is declared:

```python
    profiles = re.search(r"^PROFILES:.*?\n\}", src, re.S | re.M)
```

- [ ] **Step 6: Teach the attribute audit about mixins**

Add above `check_attributes`:

```python
def class_families(tree: ast.Module) -> dict[str, set[str]]:
    """
    Class name -> every class in the corpus that shares an instance with it.

    A mixin's methods run on the instance of the class that mixes them in, so
    an attribute that class assigns IS assigned as far as the mixin is
    concerned, and a sibling mixin's method IS a method of the same object.
    Without this, splitting a class across files reports every attribute it
    uses as one it never sets.
    """
    bases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases[node.name] = {b.id for b in node.bases
                                if isinstance(b, ast.Name)}

    def ancestors(name: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        if name in seen:
            return seen
        seen.add(name)
        for base in bases.get(name, ()):
            if base in bases:
                ancestors(base, seen)
        return seen

    lineage = {name: ancestors(name) for name in bases}
    return {name: set().union(*(lineage[d] for d in bases if name in lineage[d]))
            for name in bases}
```

In `check_attributes`, collect what every class in the corpus assigns, and forgive a name
the family assigns. Replace the per-class assignment gathering with a helper and use it
twice:

```python
def _assigned_in(cls: ast.ClassDef) -> dict[str, int]:
    """Attribute -> the line that first assigns it, methods included."""
    assigned: dict[str, int] = {}
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assigned.setdefault(item.name, item.lineno)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            assigned.setdefault(item.target.id, item.lineno)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, item.lineno)
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self" and isinstance(node.ctx, ast.Store):
            assigned.setdefault(node.attr, node.lineno)
    return assigned


def check_attributes(src: str, tree: ast.Module, f: Findings) -> None:
    families = class_families(CORPUS_TREE)
    corpus_assigned = {c.name: _assigned_in(c) for c in ast.walk(CORPUS_TREE)
                       if isinstance(c, ast.ClassDef)}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        assigned = _assigned_in(cls)
        read: set[str] = set()
        for node in ast.walk(cls):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self" \
                    and not isinstance(node.ctx, ast.Store):
                read.add(node.attr)
        module_read = {n.attr for n in ast.walk(CORPUS_TREE)
                       if isinstance(n, ast.Attribute)
                       and isinstance(n.ctx, ast.Load)}
        for name, line in sorted(assigned.items(), key=lambda kv: kv[1]):
            if name.startswith("__") or name in read or name in module_read:
                continue
            f.error(where(f"L{line}: {cls.name}.{name} is assigned but never "
                          f"read -- leftover state"))
        family = set().union(*(set(corpus_assigned.get(c, {}))
                               for c in families.get(cls.name, {cls.name})))
        for name in sorted(read - set(assigned) - family):
            if name.startswith("_") and not hasattr(object, name):
                # Reading state the constructor never sets is how a test that
                # bypasses __init__ blows up at runtime.
                f.warn(f"{cls.name}.{name} is read but never assigned in "
                       f"this class")
```

- [ ] **Step 7: Run the tests and compare the report against the baseline**

```bash
python -m unittest test_btc_5m.TestCoherenceCorpus test_btc_5m.TestNoSilentFailures -v
```

Expected: PASS.

```bash
python coherence.py
```

Expected: exit 0, `43 warning(s)`, **no errors** — the same 43 the baseline had, since
the corpus is still the same two files. If the count moved, diff it against the
baseline report before going further:

```bash
python coherence.py --source btc_5m_predictor.py --source ws_feeds.py > /tmp/after.txt
```

- [ ] **Step 8: Commit**

```bash
git add coherence.py test_btc_5m.py && git commit -m "$(cat <<'EOF'
Ask coherence about the project, not about the first file it was given

Four of its checks read sources[0] only: the Config audit, the CLI audit, the
default-profile audit and the mode-flag audit. That was the whole bot while the
bot was one file. It is about to become a package with a facade, and those
checks would then read a facade that declares no Config, no parser and no
profile table, and report nothing at all -- a gate that passes because it
looked in an empty room. They now read the merged corpus.

The attribute audit learns about inheritance at the same time, so a class split
into mixins does not report every attribute its host assigns as missing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Let conformance check every file that talks to the venue

`conformance.py` parses one file for `self._request(...)` calls. The venue client is
about to become six files, and a checker pointed at the facade would find zero calls and
report "all calls conform" — the worst possible answer from a schema gate.

Baseline, measured on the monolith: **16 call sites across 15 endpoints**. That is the
number that must survive the split.

**Files:**
- Modify: `conformance.py`
- Test: `test_btc_5m.py` (`TestSchemaConformance`)

**Interfaces:**
- Consumes: `coherence.bot_sources`
- Produces: `conformance.collect(sources) -> tuple[list[CallSite], dict[str, set[str]]]`;
  `check(connector, sources)` takes a list; `--source` repeats and defaults to the whole
  bot. `CallSite` gains a `source` field naming the file it was found in.

- [ ] **Step 1: Write the failing test**

Add to `TestSchemaConformance`:

```python
    def test_every_request_call_is_found_in_every_source(self):
        """
        The client is several files. A checker that reads one of them and
        reports "all calls conform" is worse than no checker.
        """
        import ast as _ast, os as _os, sys as _sys
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        import coherence, conformance
        here = _os.path.dirname(_os.path.abspath(__file__))
        sources = coherence.bot_sources(here)
        calls, reads = conformance.collect(sources)

        expected = 0
        for path in sources:
            with open(path, encoding="utf-8") as fh:
                tree = _ast.parse(fh.read())
            expected += sum(
                1 for n in _ast.walk(tree)
                if isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr == "_request"
                and n.args and isinstance(n.args[0], _ast.Constant))
        self.assertEqual(len(calls), expected)
        self.assertEqual(len(calls), 16, "the bot's venue calls changed count")
        self.assertEqual(len({c.endpoint for c in calls}), 15)
        self.assertIn("get_quote", reads)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m unittest test_btc_5m.TestSchemaConformance -v
```

Expected: FAIL, `module 'conformance' has no attribute 'collect'`.

- [ ] **Step 3: Make conformance read a list of sources**

In `conformance.py`, give `CallSite` the file it came from:

```python
@dataclass
class CallSite:
    endpoint: str
    params: set[str]
    dynamic: bool           # params built at runtime, cannot be fully checked
    line: int
    source: str = ""        # which file, now that the client is several
```

In `extract_calls`, stamp it — `calls.append(CallSite(endpoint, set(), False, node.lineno, os.path.basename(source_path)))` for the no-params branch and
`calls.append(CallSite(endpoint, params, dynamic, node.lineno, os.path.basename(source_path)))` for the other.

Add `collect()` under `extract_response_reads`:

```python
def collect(sources: list[str]) -> tuple[list[CallSite], dict[str, set[str]]]:
    """Every call the bot makes and every response field it reads."""
    calls: list[CallSite] = []
    reads: dict[str, set[str]] = {}
    for path in sources:
        calls += extract_calls(path)
        for endpoint, fields in extract_response_reads(path).items():
            reads.setdefault(endpoint, set()).update(fields)
    return calls, reads
```

Make `check_values` take the same list, replacing its first two lines with a loop over
`sources`, and change `check()`'s signature and its three call sites:

```python
def check(connector: str, sources: list[str]) -> int:
    ...
    interfaces = parse_interfaces(connector)
    calls, reads = collect(sources)
```

and later `value_problems = check_values(sources, constraints, calls)`. Every problem
message that names a line becomes `f"{call.source}:L{call.line}: ..."`, so a finding
says which file it is in.

Finally, default the argument to the whole bot:

```python
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--connector", default=DEFAULT_CONNECTOR)
    ap.add_argument("--source", action="append", default=None,
                    help="check this file; repeat it, or omit it for the "
                         "whole bot")
    args = ap.parse_args()
    from coherence import bot_sources
    return check(args.connector, args.source or bot_sources())
```

- [ ] **Step 4: Run the test**

```bash
python -m unittest test_btc_5m.TestSchemaConformance -v
```

Expected: PASS (`test_all_calls_conform` skips — the Node connector is not installed
here; it is not installed in the image either, which is why this extraction test
matters).

```bash
python conformance.py; echo "exit=$?"
```

Expected: `exit=2` with "Connector not found" — unchanged behaviour without the
connector.

- [ ] **Step 5: Commit**

```bash
git add conformance.py test_btc_5m.py && git commit -m "$(cat <<'EOF'
Check every file that calls the venue, not the first one

conformance reads one source for self._request calls. The client is about to
become six files, and a checker pointed at the facade would find no calls and
print "all calls conform" -- a schema gate that passes by looking at nothing.
It now takes the whole bot, and a finding says which file it is in.

The count is pinned at 16 calls across 15 endpoints so the split cannot quietly
lose one: the connector is absent here and in the image, so the conformance run
itself skips, and nothing else would notice.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Point the source meta-tests at the package

`TestNoSilentFailures` and `TestNoRawTracebacks` read `inspect.getsource(m)`. After the
split that is the facade: a hundred lines of imports, containing no bare `except`, no
handler that swallows an error, and no `main` to find. They would pass forever, on
nothing. Baseline: **73 except handlers and 249 definitions** in the bot's source.

**Files:**
- Modify: `test_btc_5m.py` (module-level helpers, `TestNoSilentFailures`,
  `TestNoRawTracebacks`)

**Interfaces:**
- Consumes: `coherence.package_sources`
- Produces: `ROOT`, `_package_trees()`, `_package_tree()` in the test module.

- [ ] **Step 1: Add the corpus helpers**

After the imports in `test_btc_5m.py`:

```python
ROOT = os.path.dirname(os.path.abspath(__file__))


def _package_trees():
    """
    (path, source, tree) for every file the bot itself is made of.

    Parsed one file at a time and merged, rather than concatenated: a
    `from __future__ import annotations` halfway down a joined text is not a
    module anyone can parse.
    """
    import ast as _ast
    import coherence
    out = []
    for path in coherence.package_sources(ROOT):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        out.append((path, src, _ast.parse(src)))
    return out


def _package_tree():
    """Every bot file's top-level statements, as one module to walk."""
    import ast as _ast
    return _ast.Module(
        body=[node for _, _, tree in _package_trees() for node in tree.body],
        type_ignores=[])
```

- [ ] **Step 2: Write the guard test that would catch a corpus that shrank**

Add to `TestNoSilentFailures`:

```python
    def test_the_rules_are_pointed_at_the_whole_bot(self):
        """
        These rules are worth exactly what they are read against.

        Read through the facade they would find no handlers, no main and no
        client, and every rule above would pass on an empty corpus. This is
        the test that fails instead.
        """
        import ast
        defined = {n.name for n in ast.walk(self.tree)
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for name in ("main", "Trader", "PredictionClient", "Journal",
                     "assess", "breakeven_probability"):
            self.assertIn(name, defined, f"{name} is not in view")
        self.assertGreater(len(defined), 200, "the corpus lost definitions")
        handlers = [n for n in ast.walk(self.tree)
                    if isinstance(n, ast.ExceptHandler)]
        self.assertGreater(len(handlers), 60,
                           f"only {len(handlers)} except handlers in view; "
                           f"the rules are reading a fraction of the bot")
```

- [ ] **Step 3: Run it, then prove it can fail**

```bash
python -m unittest test_btc_5m.TestNoSilentFailures -v
```

Expected: **PASS**. This guard is not red-green: it states an invariant that holds today
(`inspect.getsource(m)` is the whole bot) and must still hold after the split. A guard
that has never failed is worth nothing, so prove it bites — temporarily set
`cls.tree = ast.parse("")` in `setUpClass`, re-run:

Expected: FAIL, `main is not in view`. Then undo that edit.

- [ ] **Step 4: Read the package instead of the module**

In `TestNoSilentFailures`:

```python
    @classmethod
    def setUpClass(cls):
        import ast
        files = _package_trees()
        cls.src = "\n".join(src for _, src, _ in files)
        cls.tree = ast.Module(
            body=[n for _, _, tree in files for n in tree.body],
            type_ignores=[])
```

In `TestNoRawTracebacks`, both tests replace `tree = ast.parse(inspect.getsource(m))`
with `tree = _package_tree()` (and drop the now-unused `inspect` import from each).

- [ ] **Step 5: Run them**

```bash
python -m unittest test_btc_5m.TestNoSilentFailures test_btc_5m.TestNoRawTracebacks -v
```

Expected: PASS, 7 tests. The corpus is identical to what `inspect.getsource(m)` returned
before, because the package does not exist yet — which is the point: the change is
proven on a corpus whose answer is already known.

- [ ] **Step 6: Commit**

```bash
git add test_btc_5m.py && git commit -m "$(cat <<'EOF'
Read the meta-rules against the bot rather than against one module

The rules that catch a swallowed error read inspect.getsource(m). That is the
whole bot today and a facade full of imports tomorrow, where there is no bare
except to find and no handler that returns without saying why -- so they would
pass forever on nothing. They now read every file the bot is made of, and a
guard test fails if that corpus ever stops containing the code it is meant to
police.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Make the deploy path ship and check the package

Two gates stage the bot into a temporary directory and run it there. Both copy a file
list that is about to be wrong — and one of them is **already** wrong:
`TestDeploymentEntrypoint` copies `btc_5m_predictor.py` alone, without `ws_feeds.py`,
which the bot has imported at module scope since the socket work landed. Reproduced
here:

```
python btc_5m_predictor.py --print-default-profile
ModuleNotFoundError: No module named 'ws_feeds'
```

On Windows those tests fail earlier (no `.sh` exec), which is why it has stayed hidden;
on Linux this should be failing the build. **Report this to the user with the Render
build log before assuming the deploy is green.**

**Files:**
- Create: `btc5m/__init__.py`
- Modify: `verify.sh`, `Dockerfile`, `checkup.sh`, `mutate.py`, `test_btc_5m.py`
  (`TestDeploymentEntrypoint`, `TestVerificationGate`, `TestDeploymentManifests`)

**Interfaces:**
- Produces: an empty `btc5m` package; a staging helper that copies the whole bot;
  `TestVerificationGate._home_of(needle)` to find the file a definition lives in.

- [ ] **Step 1: Create the package**

```bash
mkdir -p btc5m && python - <<'PY'
open("btc5m/__init__.py", "w", encoding="utf-8", newline="\n").write(
    '"""\n'
    "The bot, one responsibility per module. btc_5m_predictor.py is the entry\n"
    "point and re-exports this package's public surface.\n"
    '"""\n')
PY
```

- [ ] **Step 2: Write the failing tests**

Add to `TestDeploymentManifests`:

```python
    def test_the_image_ships_the_package(self):
        """
        An image with the entry point and not the package has no bot in it,
        and says so as an ImportError at boot rather than as a build failure.
        """
        self.assertIn("btc5m", self._read("Dockerfile"),
                      "the Dockerfile must COPY the btc5m package")
```

Add to `TestVerificationGate`:

```python
    def test_the_gate_compiles_every_file(self):
        """A syntax error inside the package must fail the gate as a syntax
        error, not arrive later dressed as a broken test."""
        text = open(self.script).read()
        self.assertIn("btc5m", text)
```

- [ ] **Step 3: Run them and watch them fail**

```bash
python -m unittest test_btc_5m.TestDeploymentManifests -v
```

Expected: FAIL on `test_the_image_ships_the_package`.
(`TestVerificationGate` cannot run on Windows; its new test is checked by reading
`verify.sh` after Step 4 and by the Render build.)

- [ ] **Step 4: Compile and analyse the whole bot in `verify.sh`**

Replace the byte-compile block:

```bash
# Every file the bot is made of. A syntax error inside the package would
# otherwise reach the unit run as an ImportError and be reported as a broken
# test rather than as broken code.
sources="btc_5m_predictor.py"
[ -f ws_feeds.py ] && sources="$sources ws_feeds.py"
if [ -d btc5m ]; then
  sources="$sources $(find btc5m -name '*.py' | sort | tr '\n' ' ')"
fi
run "byte-compile" "$PY" -m py_compile $sources
```

and the coherence block:

```bash
if [ -f coherence.py ]; then
  # No --source: coherence reads every file the bot is made of by default, so
  # this list cannot drift from the package's real shape.
  run "coherence" "$PY" coherence.py
else
  printf '  %-22s SKIP\n' "coherence"; skipped=$((skipped + 1))
fi
```

- [ ] **Step 5: Ship the package in the image**

In `Dockerfile`, after the existing `COPY` of the root files:

```dockerfile
# The bot itself. Without this the image has an entry point and no bot, and
# the first thing it does at boot is fail to import one.
COPY btc5m/ ./btc5m/
```

- [ ] **Step 6: Stage the whole bot in both gate tests**

In `TestDeploymentEntrypoint`, replace the two `_sh.copy` lines in `_run` with:

```python
        _stage_bot(self.tmp)
        _sh.copy(self.script, self.tmp)
```

In `TestVerificationGate._stage`, replace the `glob` loop with the same helper, and let
the mutation name its target:

```python
    def _stage(self, mutate=None, target="btc_5m_predictor.py"):
        import shutil as _sh, os as _os
        _stage_bot(self.tmp)
        _sh.copy(self.script, self.tmp)
        if mutate:
            path = _os.path.join(self.tmp, target)
            text = open(path, encoding="utf-8").read()
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(mutate(text))

    def _home_of(self, needle):
        """The file a definition lives in -- it moves during the split."""
        import coherence, os as _os
        for path in coherence.package_sources(self.here):
            with open(path, encoding="utf-8") as fh:
                if needle in fh.read():
                    return _os.path.relpath(path, self.here)
        self.fail(f"nothing defines {needle!r}")
```

and point `test_broken_logic_is_caught` at wherever the function now lives:

```python
    def test_broken_logic_is_caught(self):
        """A function that returns a plausible constant still fails."""
        signature = ("def breakeven_probability(price: float, fee_bps: int) "
                     "-> float:")
        self._stage(lambda t: t.replace(
            signature, signature + "\n    return 0.5"),
            target=self._home_of(signature))
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)
```

Add the staging helper next to the other module-level helpers in `test_btc_5m.py`:

```python
def _stage_bot(target_dir):
    """
    Copy the whole bot into a directory, the way a deploy would.

    Every root-level module plus the package. Copying the entry point alone
    was enough while the entry point was the bot; it stopped being enough when
    ws_feeds arrived, and the entrypoint test has been failing on Linux ever
    since -- invisibly here, because Windows cannot exec the script at all.
    """
    import shutil as _sh, os as _os, glob as _glob
    for path in _glob.glob(_os.path.join(ROOT, "*.py")):
        _sh.copy(path, target_dir)
    pkg = _os.path.join(ROOT, "btc5m")
    if _os.path.isdir(pkg):
        _sh.copytree(pkg, _os.path.join(target_dir, "btc5m"),
                     ignore=_sh.ignore_patterns("__pycache__"),
                     dirs_exist_ok=True)
```

- [ ] **Step 7: Rehearse the staged run, since the shell tests cannot run here**

```bash
python - <<'PY'
import os, shutil, subprocess, sys, tempfile
root = os.getcwd()
tmp = tempfile.mkdtemp()
for name in os.listdir(root):
    if name.endswith(".py"):
        shutil.copy(os.path.join(root, name), tmp)
shutil.copytree(os.path.join(root, "btc5m"), os.path.join(tmp, "btc5m"),
                ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
env = dict(os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
for args in (["--config", "c.json", "--db", "j.db", "--profile", "lastminute",
              "--write-config"],
             ["--config", "c.json", "--db", "j.db", "--check-config"]):
    r = subprocess.run([sys.executable, "btc_5m_predictor.py", *args], cwd=tmp,
                       env=env, capture_output=True, text=True, timeout=120)
    print(args[-1], "exit", r.returncode, r.stderr[-200:])
shutil.rmtree(tmp, ignore_errors=True)
PY
```

Expected: both exit 0. This is the half of `TestDeploymentEntrypoint` that Windows can
run, and it is the half that was broken.

- [ ] **Step 8: Carry the package into the other two tools**

In `mutate.py`, copy the project rather than three files, and resolve the target
relative to the project root:

```python
    here = os.path.dirname(os.path.abspath(__file__))
    ...
    workdir = tempfile.mkdtemp(prefix="mutate.")
    for name in os.listdir(here):
        path = os.path.join(here, name)
        if name.endswith(".py"):
            shutil.copy(path, workdir)
        elif name == "btc5m" and os.path.isdir(path):
            shutil.copytree(path, os.path.join(workdir, name),
                            ignore=shutil.ignore_patterns("__pycache__"))
    ...
    target = os.path.join(workdir,
                          os.path.relpath(os.path.abspath(args.source), here))
```

(`here` was `os.path.dirname(os.path.abspath(args.source))`, which pointed inside the
package as soon as `--source btc5m/pricing.py` was used, and which is also why
`ws_feeds.py` was never copied.)

In `checkup.sh`, add the package and the transport module to the copy list and to the
stray-file allowlist:

```bash
cp "$SRC/btc_5m_predictor.py" "$SRC/ws_feeds.py" "$SRC/test_btc_5m.py" \
   "$SRC/conformance.py" "$SRC/fuzz.py" "$SRC/mutate.py" \
   "$SRC/coherence.py" \
   "$SRC/entrypoint.sh" "$SRC/render.yaml" "$SRC/Dockerfile" \
   "$SRC/requirements.txt" "$SRC/verify.sh" .
cp -r "$SRC/btc5m" . 2>/dev/null || true
```

and add `btc5m|ws_feeds.py` to the `STRAY` allowlist pattern. **`mutate.py` and
`checkup.sh` are not run here** — mutate runs the full suite twice per mutation, and
checkup expects a sandbox that does not exist on this machine. They are changed so they
are not left broken, and verified by reading.

- [ ] **Step 9: Run the checks**

```bash
python -m unittest test_btc_5m.TestDeploymentManifests test_btc_5m.TestWsConfig test_btc_5m.TestPreflightGate test_btc_5m.TestDefaultProfileCoherence test_btc_5m.TestCoherenceCorpus -v
```

Expected: PASS.

```bash
python -c "import mutate, coherence, conformance; print('tools import')" && python coherence.py && python fuzz.py --trials 400 && python tools/split_tool.py check --baseline <BASELINE>
```

Expected: `tools import`, coherence exit 0 with 43 warnings, fuzz exit 0, check
`0 problem(s)`.

- [ ] **Step 10: Commit**

```bash
git add btc5m Dockerfile verify.sh checkup.sh mutate.py test_btc_5m.py && git commit -m "$(cat <<'EOF'
Stage the whole bot where a gate runs it, not one file of it

The entrypoint test copies btc_5m_predictor.py into a temp directory and runs
it there. That stopped being the bot when ws_feeds arrived: on Linux the staged
run dies with ModuleNotFoundError before it writes a config. Windows never saw
it because it cannot exec the script at all. Both gates now stage every root
module and the package, the verification gate compiles all of it, and the image
COPYs the package -- an image with the entry point and no bot fails at boot
rather than at build.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## The ritual every move task runs

Tasks 7 to 15 all do the same thing to different code. Rather than repeat it, each of
them runs **this ritual**, and names only its modules, its test classes and anything
specific to watch.

**R1. Move** — for each module the task lists, in the order listed:

```bash
python tools/split_tool.py move <module path>
```

Expected: `moved <module path>`. The tool refuses (and changes nothing) if a name it is
moving still depends on a name left in the facade — that is the ordering constraint
enforcing itself, not a problem to work around.

**R2. Equivalence** — every definition still the one the split started from:

```bash
python tools/split_tool.py check --baseline <BASELINE>
```

Expected: `0 problem(s)`.

**R3. Gates:**

```bash
python coherence.py && python fuzz.py --trials 400
```

Expected: coherence exit 0, **0 errors**, 43 warnings **plus or minus
magic-number warnings only** — literal counts are per file, so spreading the code
changes which literals repeat four times in one place. Any *new* error, or any new
warning that is not a `literal ... appears N times`, stops the task.

**R4. Behaviour fingerprint:**

```bash
python tools/smoke.py > tools/now.txt && python - <<'PY'
base = open("tools/baseline.txt", encoding="utf-8").read().split("\n", 1)[1]
now = open("tools/now.txt", encoding="utf-8").read()
print("identical:", base == now)
if base != now:
    import difflib
    print("\n".join(list(difflib.unified_diff(
        base.split("\n"), now.split("\n"), lineterm=""))[:40]))
PY
rm tools/now.txt
```

Expected: `identical: True`.

**R5. Tests** — the classes the task names, plus the always-set:

```
TestNoSilentFailures TestNoRawTracebacks TestCoherenceCorpus TestScriptModeSharesOneSide
```

as `python -m unittest test_btc_5m.TestX test_btc_5m.TestY ...`. Expected: OK, no
errors. **Never** run the full suite, `TestDeploymentEntrypoint` or
`TestVerificationGate` here.

**R6. Nothing lost from the suite:**

```bash
python -c "import unittest; print(unittest.TestLoader().loadTestsFromName('test_btc_5m').countTestCases())"
```

Expected: the same number as the previous task printed. (It was 1040 before Task 1;
Tasks 1 and 3 to 6 add tests, so record the number each time rather than assuming one.)

**R7. Commit** — one commit, message in the repo's voice, naming what moved and why.

---

## Task 7: Move the leaves

Nothing here depends on anything else in the bot, which is why it goes first.

**Modules (in order):**

```
btc5m/constants.py
btc5m/units.py
btc5m/venue/endpoints.py
btc5m/errors.py
btc5m/stats.py
```

**Test classes for R5:** `TestWeiUnits TestErrorClassification TestErrorSurfacing
TestEndpointMethods TestStudentT TestBatchRedeemResponses TestRequestSigning`

**Watch for:**
- This is the first `move`, so it creates `btc5m/venue/__init__.py` as well. Check both
  `__init__.py` files got the docstring from `PACKAGE_DOCS` and nothing else.
- `_as_float_or_none` moves into `btc5m/units.py`. `TestNoSilentFailures` exempts
  `*_or_none` functions from the "an except that returns must explain itself" rule and
  caps them at three statements — it now reads the new file, and the function is
  unchanged, so it stays exempt and stays under the cap.
- `LOG` moves. Everything else in the facade still refers to it through the import the
  tool inserted; `m.LOG` still resolves for the tests that attach a handler to it, and
  it is the same `logging.getLogger("btc5m")` object either way. R2 proves the identity.

- [ ] **Step 1: R1 for each module above**
- [ ] **Step 2: R2**
- [ ] **Step 3: R3**
- [ ] **Step 4: R4**
- [ ] **Step 5: R5**
- [ ] **Step 6: R6**
- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Give the bot's constants, units, addresses and errors their own modules

The first five: nothing in them depends on anything else in the bot, so they
can leave without anything following them. Every definition is copied by line
range and checked against the syntax tree it had before the move.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Move the maths

**Modules (in order):**

```
btc5m/pricing.py
btc5m/pnl.py
btc5m/sizing.py
btc5m/domain.py
```

**Test classes for R5:** `TestDigitalPricing TestBreakeven TestKelly TestWalkBook
TestSettlePnl TestWinReturn TestReservationPrice TestBracketArithmetic
TestSignalEdgeRequired TestStraddleSplit TestStraddleCompletion TestOrderPlan
TestTrendArithmetic TestEdgeThresholds TestBoundaryConditions TestTieSettlement
TestParseAsks TestParseBids`

**Watch for:**
- `pricing`, `sizing` and `domain` all take `cfg: Config` in annotations while `Config`
  is still in the facade. The tool writes those as `if TYPE_CHECKING: from btc5m.config
  import Config` — a module that does not exist until Task 9. That is correct and inert:
  `from __future__ import annotations` means the annotation is never evaluated, and the
  block never runs. R2's undefined-name check ignores it for the same reason.
- `fuzz.py` calls `m.digital_up_probability`, `m.kelly_stake`, `m.walk_book`,
  `m.settle_pnl`, `m.breakeven_probability`, `m.buy_reservation_price`,
  `m.sell_reservation_price`, `m.win_return`, `m.kelly_multiple` and `m.Side` through the
  facade. R3 runs it; if the facade stopped re-exporting one of them, fuzz says so.

- [ ] **Step 1: R1** — [ ] **Step 2: R2** — [ ] **Step 3: R3** — [ ] **Step 4: R4** —
      [ ] **Step 5: R5** — [ ] **Step 6: R6**
- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Separate what a contract is worth from how much to stake on it

Pricing answers what the market should cost, sizing answers how much of the
bankroll may back that answer, pnl answers what a settled trade paid, and
domain holds the things all three talk about. They were one stretch of file
where reading any of them meant scrolling past the other three.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Move the configuration

**Modules (in order):**

```
btc5m/config.py
btc5m/profiles.py
btc5m/config_file.py
```

**Test classes for R5:** `TestConfigValidation TestConfigFile TestHotReload
TestProfileDefaults TestProfileRiskCoherence TestLimitConfig TestScalpConfig
TestStraddleConfig TestLastMinuteConfig TestWsConfig TestConvexProfile TestMicroProfile
TestFavoriteProfile TestModeIsNotHardcoded TestDefaultProfileCoherence
TestNoBakedInValues TestBlendCapIsPerProfile TestBlendedPriceCeiling TestTradingModeEnv`

**Watch for:**
- This is the task where Task 3 earns its keep. `Config`, `PROFILES` and
  `DEFAULT_PROFILE` leave the facade, and coherence's Config audit, CLI audit and
  default-profile audit now find them only because they read the corpus. **If coherence
  prints "no Config class found" or "DEFAULT_PROFILE is not declared", Task 3 regressed
  — stop.**
- `Config.__post_init__` calls `max_price_for_return`, which moved in Task 8, and reads
  `DEFAULT_ENDPOINTS` and `DEFAULT_ROUND_SECONDS` from Task 7. If the tool refuses,
  something in those tasks did not land.
- Two manifests carry the default profile as a literal and name where it is declared.
  Update the comments (the values do not change):
  - `Dockerfile`: `# Must match DEFAULT_PROFILE in btc5m/profiles.py -- coherence.py
    asserts it.`
  - `render.yaml`: `# must match DEFAULT_PROFILE in btc5m/profiles.py`
  `TestDefaultProfileCoherence` and coherence's `check_default_profile` both compare the
  literals, not the comments, so this is documentation — but a comment pointing at a file
  that no longer declares it is exactly the kind of stale artifact this project checks
  for.

- [ ] **Step 1: R1** — [ ] **Step 2: Update the two manifest comments** —
      [ ] **Step 3: R2** — [ ] **Step 4: R3** — [ ] **Step 5: R4** — [ ] **Step 6: R5**
      — [ ] **Step 7: R6**
- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Split what a setting is from what a strategy sets it to

Config is the schema and its validation, profiles are the strategies as named
sets of values, and config_file is how either of them survives a restart. The
three answer different questions and changed for different reasons, which is
the whole argument for them not sharing a file.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Move risk, the record, and the gates

**Modules (in order):**

```
btc5m/volatility.py
btc5m/risk.py
btc5m/journal.py
btc5m/assessment.py
btc5m/paper.py
```

**Test classes for R5:** `TestRiskManager TestCalibrationBreaker TestVolatilityCache
TestTailEstimation TestClampedSigmaGuard TestRawSigmaDiagnostic TestTrendDetection
TestJournal TestDiagnose TestBiasReport TestPerProfileReport TestBufferReport
TestPerMarketFeeInDiagnostics TestEvaluate TestBufferGate TestGatesSeeThePricePaid
TestReturnFloor TestSmallAccountSizing TestPaperRestingOrders
TestVolatilityReadsMarketData`

**Watch for:**
- Several of these classes open sqlite journals. If a teardown fails with
  `PermissionError [WinError 32]`, that is the Windows file-lock noise, not this change —
  `_close_journals` sweeps live instances and the `gc.collect()` before the sweep is
  load-bearing. Do not "fix" it.
- `assess` pulls in fourteen other names; it is the largest single dependency fan in the
  bot and the best evidence the boundaries are right. If the tool refuses here, a name it
  needs was mapped to a module later than `btc5m/assessment.py` in `MODULES` — reorder
  the map rather than the code.

- [ ] **Step 1: R1** — [ ] **Step 2: R2** — [ ] **Step 3: R3** — [ ] **Step 4: R4** —
      [ ] **Step 5: R5** — [ ] **Step 6: R6**
- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Separate measuring the market from deciding whether to trade it

Volatility measures, risk refuses, the journal records, assessment decides and
paper pretends. Five reasons to change, previously one file to change them in.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Split the venue client

`PredictionClient` is 1,160 lines covering five different conversations with the venue.
Its methods move into one mixin per conversation; the class keeps its name, its
`__init__`, its class attributes and its parsers.

**Modules (in order):**

```
btc5m/venue/spot.py
btc5m/venue/account.py
btc5m/venue/markets.py
btc5m/venue/orders.py
btc5m/venue/settlement.py
btc5m/venue/client.py
```

**Test classes for R5:** `TestParseRound TestVariantParsing TestSymbolMapping
TestRequestSigning TestEndpointMethods TestBalanceLookup TestPredictionWalletBalance
TestFundingSourceDerivation TestBankrollViability TestMinimumDiscovery
TestQuoteErrorClassification TestQuoteValidation TestStrictFieldParsing
TestOrderResponseHandling TestHostilePayloads TestLimitQuoting TestOrderStateAndCancel
TestRedemption TestVenueDerivedParameters TestPerMarketFee TestNoBakedInValues
TestSchemaConformance TestBookFeed`

**Watch for:**
- **Parsing stays in `client.py`, deliberately.** `apply_config` pushes settings onto
  `PredictionClient` as class attributes and `_parse_round` reads them back as
  `PredictionClient.symbols`, `PredictionClient.open_statuses` and so on, because it is a
  staticmethod validating payloads with no instance to hand. A mixin in another file
  cannot name the class that mixes it in without importing the module that imports it.
  So `client.py` owns construction, signing, transport **and** the parse-once rules,
  and the tool will refuse if anything tries to take the parsers elsewhere.
- `DEAD_ORDER_STATUSES` and `FILLED_ORDER_STATUSES` travel with the order methods,
  `DEAD_REDEEM_STATUSES` with settlement. `Trader._claim_relentlessly` reads
  `PredictionClient.DEAD_REDEEM_STATUSES`, which still resolves — through the base class
  now instead of the class body. R2 compares the member sets, so a name that failed to
  travel is reported rather than discovered at runtime.
- Five section banners inside the class describe code that has left. After the moves,
  delete these comment lines from `btc_5m_predictor.py` (they are the only thing left of
  those sections), keeping `# -- time` and `# -- transport`, which still describe what
  `client.py` does:

  ```
  # -- public spot (model input only) ---
  # -- account ---
  # -- market data ---
  # -- trading ---
  # -- settlement ---
  ```

- After `btc5m/venue/client.py` is moved, `python tools/split_tool.py leftovers` should
  report nothing from inside the class.
- Conformance's call count must be unchanged now that the calls live in four files:

  ```bash
  python -m unittest test_btc_5m.TestSchemaConformance -v
  ```

  Expected: the extraction test passes with 16 calls across 15 endpoints.

- [ ] **Step 1: R1 for the five mixins**
- [ ] **Step 2: Delete the five stale section banners**
- [ ] **Step 3: R1 for `btc5m/venue/client.py`**
- [ ] **Step 4: `python tools/split_tool.py leftovers`**
- [ ] **Step 5: R2** — [ ] **Step 6: R3** — [ ] **Step 7: R4** — [ ] **Step 8: R5** —
      [ ] **Step 9: R6**
- [ ] **Step 10: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Give each conversation with the venue its own file

PredictionClient held five of them: spot data, the account, the markets, the
orders and the settlement. They are now five mixins and a client that keeps
what only it can keep -- construction, signing, transport, and the parsers,
which read settings apply_config pushes onto the class and therefore cannot
live anywhere the class is not.

The class, its name, its constructor and every method body are unchanged; the
tool that moved them compares each one against the tree it had before.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Split the trader's machinery

The half of `Trader` that is not a strategy: money, claims, bookkeeping, the order
lifecycle, exits, settlement and scaling in.

**Modules (in order):**

```
btc5m/trader/accounting.py
btc5m/trader/claims.py
btc5m/trader/bookkeeping.py
btc5m/trader/order_lifecycle.py
btc5m/trader/exits.py
btc5m/trader/settling.py
btc5m/trader/scale_in.py
```

**Test classes for R5:** `TestSimulatedSession TestPendingOrderLifecycle TestPartialFills
TestCancelRacesFill TestLimitExits TestPaperRestingOrders TestBalanceReconciliation
TestMultiMarket TestMissedRoundReporting TestModeSwitching TestScaleIn TestScaleInSizing
TestOnlyBufferScalesIn TestSoldPositionsReachTheRiskManager TestRedemption
TestTieSettlement TestPeriodicReport TestLoopSurvivesUnexpectedFailures`

**Watch for:**
- `Trader.__init__` does not move and is not edited. `build_trader` in the test suite
  rebuilds a Trader by reflecting over `inspect.getsource(Trader.__init__)`; that is why
  the constructor stays put, and R5's session tests are what prove it still works.
- The tool rewrites `class Trader:` into `class Trader(AccountingMixin, ...):`, wrapping
  the header when it gets long. Nothing else about the class changes, and R2 compares its
  docstring too.
- Several tests read a method's source text — `inspect.getsource(m.Trader._maybe_scale_in)`
  and friends. `getsource` follows the function object, not the module, so a method that
  moved into a mixin still answers with its own body.
- coherence's attribute audit is the other thing being exercised here: a mixin reading
  `self._positions`, which `Trader.__init__` assigns, must not warn. If R3 produces a
  page of `read but never assigned` warnings, Task 3's `class_families` is not doing its
  job — stop rather than accept the noise.

- [ ] **Step 1: R1** — [ ] **Step 2: R2** — [ ] **Step 3: R3** — [ ] **Step 4: R4** —
      [ ] **Step 5: R5** — [ ] **Step 6: R6**
- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Separate the trader's machinery from the loop that drives it

Bankroll and exposure, the claim worker, pruning, the resting-order lifecycle,
exits, settlement and scaling in: seven responsibilities that happened to be
methods of the same class. They are now seven files, mixed into the same class,
with the same bodies and the same constructor.

Mixins rather than collaborators on purpose: the tests reach into these methods
and into the state they touch, so extracting objects would rewrite hundreds of
tests, which is exactly the change that cannot be proven safe. This is the step
that makes such an extraction local later.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Split the strategies, then the loop

**Modules (in order):**

```
btc5m/trader/straddle.py
btc5m/trader/model_entry.py
btc5m/trader/last_minute.py
btc5m/trader/scalp.py
btc5m/trader/core.py
```

**Test classes for R5:** `TestStraddleEntry TestStraddleCompletionBar TestStraddleConfig
TestLastMinuteEntry TestLastMinuteConfig TestScalpSignal TestScalpEntry TestScalpStops
TestScalpFlatten TestScalpConfig TestFuturesFeed TestTrendBoost TestNewLimitsActuallyBind
TestLiveQuoteGate TestSimulatedSession TestMultiMarket TestMissedRoundReporting`

**Watch for:**
- After the four strategies leave, delete the banner they left behind in the facade:
  `# -- scalping the futures lead ---`.
- `_maybe_enter` stays in `core.py`: it is the dispatcher that reads the profile and
  calls one of the four. That is the loop's decision, not a strategy's.
- `btc5m/trader/core.py` is moved last and takes `Trader` with all eleven bases. After
  it, `python tools/split_tool.py leftovers` should report nothing from inside the class.
- `TestNewCliSurface` and `TestMissedRoundReporting` assert on `inspect.getsource(
  m.Trader.run)` containing `_tally_missed`; `run` stays in core and is unchanged.

- [ ] **Step 1: R1 for the four strategies**
- [ ] **Step 2: Delete the stale scalp banner**
- [ ] **Step 3: R1 for `btc5m/trader/core.py`**
- [ ] **Step 4: `python tools/split_tool.py leftovers`**
- [ ] **Step 5: R2** — [ ] **Step 6: R3** — [ ] **Step 7: R4** — [ ] **Step 8: R5** —
      [ ] **Step 9: R6**
- [ ] **Step 10: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Give each strategy its own file and leave the loop with the loop

Four strategies -- the straddle, the model edge, the last minute and the
futures-lead scalp -- were four stretches of one class, and reading any of them
meant scrolling through the other three. The loop keeps what the loop does:
construction, mode switching, the poll, the dispatcher that picks a strategy,
and shutdown.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Move the probes and the command line

**Modules (in order):**

```
btc5m/probes.py
btc5m/cli.py
```

**Test classes for R5:** `TestMainEntryPoint TestNewCliSurface TestSymbolsArgParsing
TestSymbolsEnv TestTradingModeEnv TestPreflightGate TestAuthWait TestHostingReadiness
TestProfileDefaults TestModeSwitching TestDefaultProfileCoherence TestConfigFile`

**Watch for:**
- `main` builds its parser with a literal description, not `__doc__`, so `--help` does
  not change when `main` changes file. R4 is what proves that rather than assuming it.
- The `if __name__ == "__main__": raise SystemExit(main())` guard stays in the facade and
  now calls the `main` imported from `btc5m.cli`.
- coherence's CLI audit ("parsed but never used") reads the corpus after Task 3. If it
  starts reporting flags, it is reading `add_argument` calls in one file and `args.x`
  uses in another and Task 3's corpus change regressed — stop.
- After this task the facade contains no definitions at all: only its docstring, imports,
  the script alias and the guard. `python tools/split_tool.py check` still passes because
  every name is found in the package and re-exported.

- [ ] **Step 1: R1** — [ ] **Step 2: R2** — [ ] **Step 3: R3** — [ ] **Step 4: R4** —
      [ ] **Step 5: R5** — [ ] **Step 6: R6**
- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Separate the questions an operator asks from the trading the bot does

Preflight, whoami and discover-min probe the live venue before anyone commits
money to it; the CLI decides which of them a flag means. Neither is the bot,
and both were in the same file as it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: Make the facade a facade

Everything has moved; the entry point is now a file of leftover imports in whatever
order the moves inserted them. This task rewrites it as what it is — the entry point and
the public surface — and points `ws_feeds` at the modules that define what it borrows.

**Files:**
- Modify: `btc_5m_predictor.py`, `ws_feeds.py`

**Interfaces:**
- Produces: a facade that re-exports every name the baseline monolith defined, grouped by
  the module that now owns it.

- [ ] **Step 1: Rewrite the facade from the map**

```bash
python tools/split_tool.py facade
```

Expected: `facade rewritten`. The file is now its original docstring, `import sys`, the
script alias from Task 1, one `from btc5m.… import …` per module, and the
`if __name__ == "__main__"` guard.

- [ ] **Step 2: Say in the docstring where the code went**

Add to the end of the module docstring in `btc_5m_predictor.py`, before the closing
`"""`:

```
LAYOUT
------
This file is the entry point and the public surface: it defines nothing and
re-exports everything, so `python btc_5m_predictor.py` and
`import btc_5m_predictor as m` both still mean what they always did.

The bot is the btc5m package, one responsibility per module: constants, units,
errors and stats at the bottom; pricing, sizing and pnl above them; domain for
the value types; config, profiles and config_file for settings; volatility,
risk, journal, assessment and paper for the decision; venue/ for everything
said to the venue; trader/ for the loop and the four strategies; probes and cli
for what an operator runs. ws_feeds.py stays outside it: it is transport, and
it borrows only Side and two constants.
```

- [ ] **Step 3: Point ws_feeds at the definitions rather than the entry point**

In `ws_feeds.py`, the three lazy imports become:

```python
    from btc5m.domain import Side
```

(in `derive_asks` and `derive_bids`) and

```python
    from btc5m.constants import LOG as _LOG, WS_BOOK_VALIDATE_TOL
    from btc5m.domain import Side
```

(in `BookFeed.validate`). They stay inside the functions: `btc5m.domain` does not import
`ws_feeds`, so a module-level import would work, but keeping them where they are changes
the file by three lines instead of six and leaves the import timing exactly as it was.

The Task 1 alias stays in the facade. It is no longer load-bearing for `Side` — the
facade defines nothing to duplicate — but running the entry point as a script still
registers it under its own name, and that is one less way for a second copy of anything
to exist.

- [ ] **Step 4: Prove the whole thing**

```bash
python tools/split_tool.py check --baseline <BASELINE>
```

Expected: `0 problem(s)` over ~100 definitions.

```bash
python tools/split_tool.py leftovers
```

Expected: `0 comment line(s) left in the facade`. If any remain, they are documentation
stranded by a move: find the definition each one describes in the baseline file and paste
it above that definition in its new module, then re-run.

```bash
python coherence.py && python fuzz.py --trials 400 && python -c "import btc_5m_predictor as m; print(len([n for n in dir(m) if not n.startswith('__')]), 'names exported')"
```

Expected: coherence exit 0 with **no errors**, fuzz exit 0, and a name count at or above
the baseline's.

```bash
python tools/smoke.py > tools/now.txt && python - <<'PY'
base = open("tools/baseline.txt", encoding="utf-8").read().split("\n", 1)[1]
now = open("tools/now.txt", encoding="utf-8").read()
print("identical:", base == now)
PY
rm tools/now.txt
```

Expected: `identical: True` — the same CLI answers, the same config document, the same
numerics digest as before a single line moved.

```bash
python btc_5m_predictor.py --print-default-profile && python -m py_compile btc_5m_predictor.py ws_feeds.py $(find btc5m -name '*.py')
```

Expected: `lastminute`, and a silent compile.

- [ ] **Step 5: Run the always-set plus a spread of the suite**

```bash
python -m unittest test_btc_5m.TestNoSilentFailures test_btc_5m.TestNoRawTracebacks test_btc_5m.TestCoherenceCorpus test_btc_5m.TestScriptModeSharesOneSide test_btc_5m.TestSchemaConformance test_btc_5m.TestSimulatedSession test_btc_5m.TestMultiMarket test_btc_5m.TestStraddleEntry test_btc_5m.TestScalpEntry test_btc_5m.TestLastMinuteEntry test_btc_5m.TestMainEntryPoint test_btc_5m.TestHotReload test_btc_5m.TestBookFeed test_btc_5m.TestModeSwitching test_btc_5m.TestBalanceReconciliation -v
```

Expected: OK. Then R6 — the test count is unchanged.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Leave the entry point holding the door and nothing else

btc_5m_predictor.py now defines nothing: it is the docstring, the script alias,
one import per module of the package, and the guard that starts main. Running
it and importing it both mean exactly what they meant before, which is what
lets every test, fuzz.py, checkup.sh and entrypoint.sh carry on unchanged.

ws_feeds borrows Side and two constants from the modules that define them
rather than from the entry point, so there is one definition of each and no
route by which a second could appear.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Tell the README the truth, and take the scaffolding down

**Files:**
- Modify: `README.md`
- Delete: `tools/split_tool.py`, `tools/smoke.py`, `tools/baseline.txt`

- [ ] **Step 1: Measure, do not guess**

```bash
python - <<'PY'
import subprocess
files = subprocess.run(["git", "ls-files"], capture_output=True,
                       text=True).stdout.split()
for name in files:
    if name.endswith((".py", ".sh")):
        with open(name, encoding="utf-8") as fh:
            print(f"{sum(1 for _ in fh):>6}  {name}")
PY
```

- [ ] **Step 2: Replace the Files table in README section 2**

Keep the table's shape; the bot's row becomes the package, with the measured numbers
from Step 1:

```markdown
| File | Lines | What it is |
|---|---|---|
| `btc_5m_predictor.py` | NN | Entry point and public surface; defines nothing |
| `btc5m/` | NNNN | The bot: one responsibility per module (see below) |
| `ws_feeds.py` | NNN | Persistent WebSocket feeds behind one `MarketData` seam |
| `test_btc_5m.py` | NNNN | NNN tests across NNN classes |
| `conformance.py` | NNN | Validates every API call against Binance's own schema |
| `fuzz.py` | NNN | Property-based testing with hostile inputs |
| `coherence.py` | NNN | Finds stale artifacts, dead code, config drift |
| `mutate.py` | NNN | Mutation testing — measures test quality |
| `verify.sh` | NNN | The gate: runs before the bot is allowed to trade |
| `checkup.sh` | NNN | Full isolated verification run |
```

and add underneath it the package map — copy the *Target layout* block from
`docs/superpowers/plans/2026-09-12-single-responsibility-split.md`, minus the two files
already in the table above.

- [ ] **Step 3: Note what the two analysers read now**

In README section 9, the `coherence.py` and `conformance.py` rows get a sentence saying
they read every file the bot is made of by default, so neither needs a file list that
can drift.

- [ ] **Step 4: Remove the scaffolding**

**Skip this step if Phase 3 is being executed** — it splits the test file with the same
tool, and Task 20 removes the scaffolding at the end instead.

```bash
git rm -r tools && python -c "import btc_5m_predictor; print('still imports')"
```

A tool that proves one refactor has no second use, and a stale baseline is worse than
none. It stays in the history: `git show <BASELINE>..HEAD -- tools/` brings it back if a
later split wants it.

- [ ] **Step 5: Final gates**

```bash
python coherence.py && python fuzz.py --trials 400 && python -m unittest test_btc_5m.TestNoSilentFailures test_btc_5m.TestNoRawTracebacks test_btc_5m.TestCoherenceCorpus test_btc_5m.TestDeploymentManifests test_btc_5m.TestWsConfig test_btc_5m.TestScriptModeSharesOneSide -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Say in the README what the project is now

One file called "the bot" became a package, and the table said otherwise. The
scaffolding that moved it goes too: a tool that proves one refactor has no
second use, and it is in the history if a later one wants it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Hand the full suite to the machine that can run it**

Nothing local has run the whole suite, and two classes cannot pass here at all. The
Docker build is the gate that runs all 1,000+ tests on Linux in about 23 seconds.

**Before pushing, decide with the user:**
- `autoDeploy: true` and `TRADING_MODE: live` mean the push restarts a live trading
  worker. Push between rounds, or set `TRADING_MODE: paper` for the first deploy of the
  split, or turn autodeploy off until the build is green.
- Task 1 changes live pricing behaviour (the WebSocket book will now validate and be
  used where it previously fell back to REST). That is worth watching in the first
  session's logs rather than discovering later.

Then read the Render build log — specifically that `verify.sh` reports `unit tests OK`,
`coherence OK` and `fuzz invariants OK`, and that `TestDeploymentEntrypoint` and
`TestVerificationGate` pass there now that they stage the whole bot.

---

# Phase 3 — the test file (separately shippable, and skippable)

`test_btc_5m.py` is 11,000 lines and 125 classes: the same problem the bot had. It is a
phase of its own because it ships on its own — the bot is already split and deployable
after Task 16 — and because it is the half of this plan with the least to gain. **If it
is not being done, stop after Task 16 and run its Step 4 to remove the scaffolding.**

The rules are the same: classes move verbatim with the same tool, `test_btc_5m.py` stays
as the name everything invokes, and the test count must not move.

---

## Task 17: Teach the tool to move test code, and lift the helpers out

**Files:**
- Modify: `tools/split_tool.py`
- Create: `tests/__init__.py`, `tests/support.py`

**Interfaces:**
- Produces: `python tools/split_tool.py move <module> --from test_btc_5m.py`;
  `tests.support` holding `ROOT`, the builders, the fake clients and the profile
  helpers.

- [ ] **Step 1: Let `move` take a different source**

In `tools/split_tool.py`, replace the module-level `FACADE` use inside `move`,
`move_members` and `leftovers` with a parameter defaulting to it, add the test map, and
give `main` the flag:

```python
TEST_MODULES: dict[str, tuple[str, list[str]]] = {
    "tests/support.py": (
        "What the tests build with: configs, rounds, signals, a trader wired\n"
        "to a fake client, and the fake clients themselves.",
        ["ROOT", "build_client", "_EMPTY_CONTAINERS", "build_trader", "cfg",
         "convex_cfg", "straddle_cfg", "lastminute_cfg", "scalp_cfg",
         "_close_journals", "_stage_bot", "_package_trees", "_package_tree",
         "make_signal", "make_trader", "make_pending", "make_position",
         "make_round", "FakeClient", "QuotingClient", "ScalpClient",
         "_FakePosition"]),
    "tests/test_pricing.py": (
        "The maths: what a contract is worth, what clears a cost, how much to\n"
        "stake and what a settled trade paid.",
        ["TestDigitalPricing", "TestBreakeven", "TestKelly", "TestWalkBook",
         "TestSettlePnl", "TestStudentT", "TestWinReturn",
         "TestReservationPrice", "TestBracketArithmetic",
         "TestSignalEdgeRequired", "TestStraddleSplit", "TestStraddleCompletion",
         "TestTrendArithmetic", "TestProjectedRounds", "TestWeiUnits",
         "TestEdgeThresholds", "TestSmallAccountSizing"]),
    "tests/test_venue.py": (
        "Everything said to the venue and every payload read back from it.",
        ["TestParseRound", "TestParseAsks", "TestParseBids",
         "TestVariantParsing", "TestSymbolMapping", "TestRequestSigning",
         "TestErrorSurfacing", "TestEndpointMethods", "TestBalanceLookup",
         "TestPredictionWalletBalance", "TestFundingSourceDerivation",
         "TestMinimumDiscovery", "TestQuoteErrorClassification",
         "TestStrictFieldParsing", "TestQuoteValidation",
         "TestErrorClassification", "TestOrderResponseHandling",
         "TestHostilePayloads", "TestBatchRedeemResponses",
         "TestVenueDerivedParameters", "TestLimitQuoting",
         "TestOrderStateAndCancel", "TestOrderPlan", "TestAuthWait",
         "TestPerMarketFee", "TestRedemption"]),
    "tests/test_config.py": (
        "Settings: what they mean, which profile sets them, and what happens\n"
        "when the file on disk changes underneath a running bot.",
        ["TestConfigValidation", "TestConvexProfile", "TestMicroProfile",
         "TestFavoriteProfile", "TestProfileDefaults", "TestProfileRiskCoherence",
         "TestNoBakedInValues", "TestConfigFile", "TestHotReload",
         "TestLimitConfig", "TestScalpConfig", "TestScalpProfile",
         "TestStraddleConfig", "TestLastMinuteConfig", "TestWsConfig",
         "TestBlendCapIsPerProfile", "TestHostingReadiness"]),
    "tests/test_risk.py": (
        "What the bot measures about the market and what makes it stop.",
        ["TestRiskManager", "TestCalibrationBreaker", "TestBankrollViability",
         "TestVolatilityCache", "TestTailEstimation", "TestClampedSigmaGuard",
         "TestRawSigmaDiagnostic", "TestTrendDetection",
         "TestVolatilityReadsMarketData"]),
    "tests/test_journal.py": (
        "The record of what was traded, and every report read off it.",
        ["TestJournal", "TestBiasReport", "TestPerProfileReport",
         "TestBufferReport", "TestDiagnose", "TestPerMarketFeeInDiagnostics",
         "TestPeriodicReport"]),
    "tests/test_assessment.py": (
        "The gates between a round and a position.",
        ["TestEvaluate", "TestBufferGate", "TestBoundaryConditions",
         "TestGatesSeeThePricePaid", "TestReturnFloor",
         "TestBlendedPriceCeiling", "TestNewLimitsActuallyBind",
         "TestOnlyBufferScalesIn"]),
    "tests/test_trader.py": (
        "The loop and its machinery: sessions, positions, resting orders,\n"
        "exits, settlement, reconciliation and scaling in.",
        ["TestSimulatedSession", "TestLiveQuoteGate", "TestTieSettlement",
         "TestScaleIn", "TestScaleInSizing", "TestModeSwitching",
         "TestMultiMarket", "TestBalanceReconciliation",
         "TestPendingOrderLifecycle", "TestPartialFills", "TestCancelRacesFill",
         "TestPaperRestingOrders", "TestLimitExits", "TestMissedRoundReporting",
         "TestLoopSurvivesUnexpectedFailures",
         "TestSoldPositionsReachTheRiskManager", "TestTrendBoost"]),
    "tests/test_strategies.py": (
        "The four strategies, entered end to end against a fake venue.",
        ["TestStraddleEntry", "TestStraddleCompletionBar", "TestLastMinuteEntry",
         "TestScalpSignal", "TestScalpEntry", "TestScalpStops",
         "TestScalpFlatten"]),
    "tests/test_ws_feeds.py": (
        "The sockets: connection, books, spot, futures and recycling.",
        ["TestWsConnection", "TestBookFeed", "TestSpotFeed", "TestMarketData",
         "TestFuturesFeed", "TestWsRecycle", "TestScriptModeSharesOneSide"]),
    "tests/test_cli.py": (
        "What each command does, and what the environment may override.",
        ["TestMainEntryPoint", "TestTradingModeEnv", "TestSymbolsArgParsing",
         "TestSymbolsEnv", "TestNewCliSurface", "TestPreflightGate"]),
    "tests/test_source_rules.py": (
        "The rules the source itself must obey, and the tools that check them.",
        ["TestNoSilentFailures", "TestNoRawTracebacks", "TestSchemaConformance",
         "TestPropertyInvariants", "TestCoherenceCorpus"]),
    "tests/test_deployment.py": (
        "The deploy path: the entrypoint, the gate, and the manifests that\n"
        "have to agree with the module.",
        ["TestDeploymentEntrypoint", "TestDefaultProfileCoherence",
         "TestVerificationGate", "TestModeIsNotHardcoded",
         "TestDeploymentManifests"]),
}
```

`main` grows `--from` on the `move` parser, defaulting to `btc_5m_predictor.py`, and
`move` looks the module up in `TEST_MODULES` when the source is not the facade. The
package docstrings gain `"tests": "The suite, one subject per module. test_btc_5m.py
imports all of it, so `python -m unittest test_btc_5m` still means the whole suite."`

- [ ] **Step 2: Move the helpers**

```bash
python tools/split_tool.py move tests/support.py --from test_btc_5m.py
```

- [ ] **Step 3: Fix the two things a verbatim move cannot fix**

`ROOT` moves into `tests/support.py`, where `__file__` is one directory deeper:

```python
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

and every test that computes the project directory for itself must use it instead of its
own `__file__`. There are eleven, all spelled identically:

```bash
python - <<'PY'
import re
path = "test_btc_5m.py"
text = open(path, encoding="utf-8").read()
old = "_os.path.dirname(_os.path.abspath(__file__))"
print("sites:", text.count(old))
text = text.replace(old, "ROOT")
open(path, "w", encoding="utf-8", newline="\n").write(text)
PY
```

Expected: `sites: 11`. (`_package_trees` and `_stage_bot` already use `ROOT`.)

- [ ] **Step 4: Run a spread of the suite**

```bash
python -m unittest test_btc_5m.TestNoSilentFailures test_btc_5m.TestCoherenceCorpus test_btc_5m.TestSimulatedSession test_btc_5m.TestParseRound test_btc_5m.TestScalpEntry test_btc_5m.TestDeploymentManifests test_btc_5m.TestWsConfig -v
```

Expected: OK. Then R6: the count is unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Lift the suite's builders into a module of their own

Eleven thousand lines of tests start with four hundred lines of fixtures that
every class below uses. They move first, because nothing else can move until
they have somewhere to move to.

The eleven tests that locate the project through their own __file__ now ask
support.ROOT instead: the file is about to be one directory deeper, and a test
that reads the Dockerfile from the wrong directory skips itself rather than
failing, which is the worst way for a gate to break.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 18: Move the tests, subject by subject

**Modules (in order — `tests/test_venue.py` first, because six other modules use
`TestParseRound.topic()`):**

```
tests/test_venue.py
tests/test_pricing.py
tests/test_config.py
tests/test_risk.py
tests/test_journal.py
tests/test_assessment.py
tests/test_trader.py
tests/test_strategies.py
tests/test_ws_feeds.py
tests/test_cli.py
tests/test_source_rules.py
tests/test_deployment.py
```

each as `python tools/split_tool.py move <module> --from test_btc_5m.py`.

**Watch for:**
- After every move, `test_btc_5m.py` keeps an explicit import of the classes that left,
  which is what keeps `python -m unittest test_btc_5m.TestKelly` working, and what makes
  the aggregator collect each class exactly once.
- A class imported into another test module for its helpers (`TestParseRound`) will be
  collected twice if that module is run on its own — `python -m unittest
  tests.test_config` would run it as well. Through `test_btc_5m` it is collected once,
  which is how every gate runs the suite. Do not "fix" this by moving `topic()`.
- After each move: `python -c "import unittest; print(unittest.TestLoader().loadTestsFromName('test_btc_5m').countTestCases())"`. **The number must not move.** A
  drop means a class stopped being imported; a rise means one is being collected twice.

- [ ] **Step 1: Move each module in the order above, checking the count after each**
- [ ] **Step 2: Run a spread across the new modules**

```bash
python -m unittest test_btc_5m.TestKelly test_btc_5m.TestParseRound test_btc_5m.TestConfigValidation test_btc_5m.TestRiskManager test_btc_5m.TestJournal test_btc_5m.TestEvaluate test_btc_5m.TestSimulatedSession test_btc_5m.TestScalpEntry test_btc_5m.TestBookFeed test_btc_5m.TestMainEntryPoint test_btc_5m.TestNoSilentFailures test_btc_5m.TestDeploymentManifests -v
```

Expected: OK.

- [ ] **Step 3: Run two of the new modules directly, to prove they stand alone**

```bash
python -m unittest tests.test_pricing tests.test_risk -v
```

Expected: OK.

- [ ] **Step 4: The gates**

```bash
python coherence.py && python fuzz.py --trials 400 && python tools/split_tool.py check --baseline <BASELINE>
```

Expected: all clean. (`coherence` does not read the tests; `check` does not either. They
are run because this task must not have touched the bot.)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Give each subject its own test file

One hundred and twenty-five classes in one file, where finding the tests for a
change meant searching for a class name and hoping. They are now twelve modules
named for what they test, and test_btc_5m.py imports all of them -- so
`python -m unittest test_btc_5m`, the deploy gate, mutate.py and checkup.sh all
still mean the whole suite, and `test_btc_5m.TestKelly` still means that class.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 19: Ship the tests the same way the bot is shipped

The suite is now a package, and three places still name it as a file.

**Files:** `Dockerfile`, `test_btc_5m.py` (`_stage_bot`), `mutate.py`, `checkup.sh`

- [ ] **Step 1: Write the failing test**

Add to `TestDeploymentManifests`:

```python
    def test_the_image_ships_the_suite(self):
        """
        The image runs the suite at build AND at boot. Half a suite copied in
        is a gate that passes because most of it was not there.
        """
        self.assertIn("tests/", self._read("Dockerfile"))
```

- [ ] **Step 2: Run it and watch it fail**

```bash
python -m unittest test_btc_5m.TestDeploymentManifests -v
```

Expected: FAIL.

- [ ] **Step 3: Copy the suite everywhere the bot is copied**

`Dockerfile`, after the package:

```dockerfile
COPY tests/ ./tests/
```

`_stage_bot` in `tests/support.py` gains the same treatment as `btc5m`:

```python
    for name in ("btc5m", "tests"):
        source = _os.path.join(ROOT, name)
        if _os.path.isdir(source):
            _sh.copytree(source, _os.path.join(target_dir, name),
                         ignore=_sh.ignore_patterns("__pycache__"),
                         dirs_exist_ok=True)
```

`mutate.py`'s copy loop: `elif name in ("btc5m", "tests") and os.path.isdir(path):`

`checkup.sh`: `cp -r "$SRC/btc5m" "$SRC/tests" . 2>/dev/null || true`, and `tests` added
to the `STRAY` allowlist.

- [ ] **Step 4: Verify**

```bash
python -m unittest test_btc_5m.TestDeploymentManifests test_btc_5m.TestWsConfig -v && python -c "import mutate; print('mutate imports')"
```

Expected: PASS. Re-run the Task 6 staging rehearsal (it copies `*.py` and the package;
extend it with `tests`) to confirm a staged directory can still run
`python -m unittest test_btc_5m`:

```bash
python - <<'PY'
import os, shutil, subprocess, sys, tempfile
root = os.getcwd()
tmp = tempfile.mkdtemp()
for name in os.listdir(root):
    if name.endswith(".py"):
        shutil.copy(os.path.join(root, name), tmp)
for name in ("btc5m", "tests"):
    shutil.copytree(os.path.join(root, name), os.path.join(tmp, name),
                    ignore=shutil.ignore_patterns("__pycache__"))
env = dict(os.environ, BINANCE_API_KEY="k", BINANCE_API_SECRET="s")
r = subprocess.run([sys.executable, "-c",
                    "import unittest;print(unittest.TestLoader()"
                    ".loadTestsFromName('test_btc_5m').countTestCases())"],
                   cwd=tmp, env=env, capture_output=True, text=True)
print("staged suite loads:", r.stdout.strip(), r.stderr[-300:])
shutil.rmtree(tmp, ignore_errors=True)
PY
```

Expected: the same test count as R6 prints in the repository.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "$(cat <<'EOF'
Ship the whole suite wherever the suite is run

The image runs the tests at build time and again at boot, and the stage-and-run
gates copy them into a temp directory. All of them named a file that is now a
package, which would have meant a gate passing on the fraction of the suite it
managed to copy.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 20: Take the scaffolding down (Phase 3 ending)

- [ ] **Step 1: Remove the tool**

```bash
git rm -r tools && python -c "import btc_5m_predictor; print('still imports')"
```

- [ ] **Step 2: Update the README's Files table for the tests package**

Re-measure with the Task 16 Step 1 command; `test_btc_5m.py` becomes the aggregator row
and `tests/` the suite row, with the class and test counts from R6.

- [ ] **Step 3: Final gates**

```bash
python coherence.py && python fuzz.py --trials 400 && python -m unittest test_btc_5m.TestNoSilentFailures test_btc_5m.TestNoRawTracebacks test_btc_5m.TestCoherenceCorpus test_btc_5m.TestSchemaConformance test_btc_5m.TestDeploymentManifests test_btc_5m.TestScriptModeSharesOneSide -v
```

Expected: all pass.

- [ ] **Step 4: Commit, then hand the full run to Render** (Task 16 Step 7's cautions
      apply again — a push restarts a live worker).

```bash
git add -A && git commit -m "$(cat <<'EOF'
Take down the scaffolding the split stood on

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```
