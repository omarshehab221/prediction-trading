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
    "tests": "The suite, one subject per module. test_btc_5m.py imports all of "
             "it, so `python -m unittest test_btc_5m` still means the whole suite.",
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

# The suite, split the same way, by subject.
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

    def record(into: dict[str, list[str]], name: str,
               strict: bool = True) -> None:
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
            if not strict:
                # Annotation-only, and nothing at module scope defines it --
                # a name imported inside the function that uses it, say.
                # `from __future__ import annotations` means it is never
                # evaluated, so there is nothing to import for it here.
                return
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
        record(typed, name, strict=False)

    def group(mod: str) -> int:
        head = mod.removeprefix("!import ").split(" ")[0].split(".")[0]
        if head.startswith("btc5m") or head in (
                "ws_feeds", "btc_5m_predictor", "tests", "coherence",
                "conformance"):
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


def move(module: str, source: str = FACADE) -> None:
    if module in MIXINS:
        move_members(module)
        return
    doc, names = MODULES.get(module) or TEST_MODULES[module]
    src, tree = load(source)
    lines = src.split("\n")
    table = symtable.symtable(src, source, "exec")
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
    write(source, "\n".join(
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
    mover.add_argument("module",
                       choices=list(MODULES) + list(MIXINS) + list(TEST_MODULES))
    mover.add_argument("--from", dest="source", default="btc_5m_predictor.py",
                       help="the file to move the definitions out of")
    checker = sub.add_parser("check")
    checker.add_argument("--baseline", required=True)
    sub.add_parser("leftovers")
    sub.add_parser("facade")
    args = ap.parse_args(argv)

    if args.cmd == "move":
        move(args.module, os.path.join(ROOT, args.source))
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
