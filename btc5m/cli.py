"""The command line: what each flag means and what it runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from btc5m.config_file import (
    ConfigStore,
    build_config,
    default_config_document,
)
from btc5m.constants import LOG
from btc5m.errors import ApiError, ErrorKind
from btc5m.journal import Journal
from btc5m.probes import discover_min, preflight, whoami
from btc5m.profiles import DEFAULT_PROFILE, PROFILES
from btc5m.trader.core import Trader

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

def _parse_symbols_arg(raw: str) -> tuple[str, ...]:
    """
    Comma-separated tickers -> a normalised tuple, e.g. "btc,eth" -> BTC,ETH.

    Shared by --symbols and the SYMBOLS environment variable so the two
    cannot silently drift apart. An empty or blank string yields (), which
    means "no restriction" -- the same as leaving the setting unset.
    """
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BTC 5m prediction market trader")
    # Tri-state on purpose. Left unset, the config file governs and the mode
    # can be changed by editing it while the bot runs. Set either way, the
    # flag pins the mode and file edits to `live` are ignored with a warning,
    # so a pinned mode can never appear to change when it cannot.
    ap.add_argument("--live", dest="live", action="store_const", const=True,
                    default=None, help="trade real money (pins the mode)")
    ap.add_argument("--paper", dest="live", action="store_const", const=False,
                    help="simulate only (pins the mode)")
    ap.add_argument("--preflight", action="store_true",
                    help="probe endpoints with your keys and exit")
    ap.add_argument("--calibration-report", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="is the edge real? compares realised win rate "
                         "against the breakeven for the price paid")
    ap.add_argument("--report-every", type=int, default=0,
                    help="log the calibration report every N settled trades; "
                         "useful when hosted, where the journal is hard to read")
    ap.add_argument("--report-profile", default=None,
                    help="restrict the report to one profile; default is "
                         "every profile in the journal, separately")
    # No default: an argparse default would always beat the file's
    # active_profile, making that setting dead exactly as --kelly's 0.25
    # default silently overrode every profile's kelly_fraction.
    ap.add_argument("--profile", choices=sorted(PROFILES), default=None,
                    help="buffer = big buffer late in the round; micro = "
                         "small accounts (default); favorite = favourites; "
                         "balanced = symmetric; convex = longshots")
    ap.add_argument("--scale-in", action="store_true", default=None,
                    help="open small, then top up while the round stays "
                         "favourable (never after a loss)")
    ap.add_argument("--no-scale-in", dest="scale_in", action="store_false")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated markets to RESTRICT trading to, "
                         "e.g. BTCUSDT,ETHUSDT. Default: none, meaning every "
                         "5m up/down market the venue lists is discovered "
                         "and traded automatically. Each trades "
                         "independently: its own position slot, loss streak "
                         "and calibration. Same as the SYMBOLS environment "
                         "variable; this flag wins if both are set.")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="how many markets may hold a position at once")
    ap.add_argument("--report-symbol", default=None,
                    help="restrict a report to one market")
    ap.add_argument("--min-buffer", type=float, default=None,
                    help="override the buffer gate, in sigmas of the time "
                         "remaining")
    ap.add_argument("--min-return", type=float, default=None,
                    help="minimum net profit per unit staked on a win, after "
                         "fees; 0.25 means a win must pay at least 25%% and "
                         "caps the fill price accordingly. 0 disables it.")
    ap.add_argument("--trend-follow", dest="trend_follow",
                    action="store_true", default=None,
                    help="enter earlier and stake more while the underlying "
                         "keeps running the same way")
    ap.add_argument("--no-trend-follow", dest="trend_follow",
                    action="store_false")
    ap.add_argument("--wait-for-auth", type=float, default=None,
                    help="seconds --preflight waits for a signed request to "
                         "be accepted, so a shared outbound IP can be added "
                         "to Binance's allowlist while it retries. Same as "
                         "the AUTH_WAIT_S environment variable; this flag "
                         "wins if both are set.")
    ap.add_argument("--whoami", action="store_true",
                    help="report the outbound IP actually in use and test "
                         "whether Binance accepts it")
    ap.add_argument("--discover-min", action="store_true",
                    help="probe the venue for its real minimum order size "
                         "(quotes only, places no order) and exit")
    ap.add_argument("--fee-bps", type=int, default=None,
                    help="override the fallback fee rate; each market's own "
                         "published rate is preferred when available")
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--no-fat-tails", action="store_true",
                    help="use a Gaussian model (understates cheap contracts)")
    ap.add_argument("--kelly", type=float, default=None,
                    help="override the profile's Kelly fraction")
    ap.add_argument("--paper-bankroll", type=float, default=None,
                    help="paper starting balance; defaults to the profile's "
                         "own figure so micro does not simulate a 100 USDT "
                         "account it will never have")
    ap.add_argument("--db", default="btc5m_journal.db")
    ap.add_argument("--config", default="config.json",
                    help="configuration file; created with --write-config. "
                         "Re-read automatically whenever it changes.")
    ap.add_argument("--write-config", action="store_true",
                    help="write a complete config file with every setting "
                         "at its current value, then exit")
    ap.add_argument("--print-default-profile", action="store_true",
                    help="print the default profile name and exit")
    ap.add_argument("--check-config", action="store_true",
                    help="validate the config file and exit; touches no "
                         "network and no journal")
    ap.add_argument("--no-hot-reload", action="store_true",
                    help="read the config once and ignore later edits")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s")

    # TRADING_MODE is the environment equivalent of --live/--paper, for
    # hosted deployments where the command line is fixed by the platform.
    # An explicit flag wins over it. Validated here, before any subcommand
    # returns, so a typo is caught even by a command that ignores the mode.
    live = args.live
    env_mode = os.environ.get("TRADING_MODE", "").strip().lower()
    if env_mode and env_mode not in ("live", "paper"):
        print(f"TRADING_MODE must be 'live' or 'paper', got {env_mode!r}",
              file=sys.stderr)
        return 1
    if live is None and env_mode:
        live = env_mode == "live"
        LOG.info("Mode pinned to %s by TRADING_MODE", env_mode.upper())

    # SYMBOLS is the environment equivalent of --symbols, same reasoning and
    # the same precedence: an explicit flag wins over it.
    env_symbols = os.environ.get("SYMBOLS", "").strip()

    # AUTH_WAIT_S is the environment equivalent of --wait-for-auth. Parsed
    # here, alongside TRADING_MODE and for the same reason: a typo must be
    # caught even by a subcommand that never reads the value, or it silently
    # becomes "do not wait" and takes the next deploy down with it.
    env_auth_wait: float | None = None
    raw_auth_wait = os.environ.get("AUTH_WAIT_S", "").strip()
    if raw_auth_wait:
        try:
            env_auth_wait = float(raw_auth_wait)
        except ValueError:
            print(f"AUTH_WAIT_S must be a number of seconds, got "
                  f"{raw_auth_wait!r}", file=sys.stderr)
            return 1

    if args.print_default_profile:
        # Exists so entrypoint.sh can read the default from the single source
        # of truth instead of repeating it in shell.
        print(DEFAULT_PROFILE)
        return 0

    if args.write_config:
        if os.path.exists(args.config):
            print(f"{args.config} already exists; refusing to overwrite.",
                  file=sys.stderr)
            return 1
        with open(args.config, "w", encoding="utf-8") as fh:
            json.dump(default_config_document(args.profile or DEFAULT_PROFILE),
                      fh,
                      indent=2, sort_keys=False)
            fh.write("\n")
        print(f"Wrote {args.config} with every setting at its current value.")
        print("Edit it while the bot runs; changes are picked up within a "
              "poll interval.")
        return 0

    if args.check_config:
        # Validates without touching the network or the journal, so an edit
        # can be checked before the running bot picks it up.
        if not os.path.exists(args.config):
            print(f"{args.config} does not exist.", file=sys.stderr)
            return 1
        try:
            with open(args.config, encoding="utf-8") as fh:
                document = json.load(fh)
            checked = build_config(document, api_key="x", api_secret="x",
                                   live=None, db_path=args.db,
                                   profile=args.profile)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            return 1
        print(f"OK: {args.config} is valid.")
        print(f"  profile        {checked.profile_name}")
        print(f"  entry band     {checked.min_entry_price:.2f}"
              f"-{checked.max_entry_price:.2f}")
        if checked.straddle:
            print(f"  max stake      "
                  f"{checked.straddle_stake_pct:.0%} of bankroll per leg, "
                  f"{checked.straddle_stake_pct * 2:.0%} per round")
        elif checked.last_minute:
            print(f"  max stake      "
                  f"{checked.last_minute_stake_pct:.0%} of bankroll "
                  f"per round")
            ceiling = ("no ceiling"
                       if checked.last_minute_max_price >= 1.0
                       else f"never above "
                            f"{checked.last_minute_max_price:.2f}")
            print(f"  entry rule     the dearer side at "
                  f"{checked.last_minute_price_floor:.2f}+ from "
                  f"{checked.last_minute_start_s:.0f}s, at any price from "
                  f"{checked.last_minute_fallback_s:.0f}s, nothing under "
                  f"{checked.last_minute_deadline_s:.0f}s; {ceiling}")
        else:
            print(f"  max stake      {checked.max_stake_pct:.0%} of bankroll")
        print(f"  min buffer     {checked.min_buffer_sigmas} sigma")
        print(f"  daily limit    {checked.daily_loss_limit_pct:.0%}")
        return 0

    if args.calibration_report:
        print(Journal(args.db).calibration_report(args.report_profile))
        return 0

    if args.diagnose:
        print(Journal(args.db).diagnose(args.report_profile,
                                        args.report_symbol))
        return 0

    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "")
    if not key or not secret:
        print("Set BINANCE_API_KEY and BINANCE_API_SECRET.", file=sys.stderr)
        return 1

    overrides: dict = {}
    if args.kelly is not None:
        overrides["kelly_fraction"] = args.kelly
    if args.min_edge is not None:
        overrides["min_edge"] = args.min_edge
    if args.fee_bps is not None:
        overrides["fee_bps"] = args.fee_bps
    if args.paper_bankroll is not None:
        overrides["paper_start_bankroll"] = args.paper_bankroll
    if args.min_buffer is not None:
        overrides["min_buffer_sigmas"] = args.min_buffer
    if args.min_return is not None:
        overrides["min_win_return"] = args.min_return
    if args.trend_follow is not None:
        overrides["trend_follow"] = args.trend_follow
    # AUTH_WAIT_S is the environment equivalent of --wait-for-auth, for hosted
    # deployments where the command line is fixed by the platform. The flag
    # wins when both are set, matching --symbols and --live.
    if args.wait_for_auth is not None:
        overrides["auth_wait_timeout_s"] = args.wait_for_auth
    elif env_auth_wait is not None:
        overrides["auth_wait_timeout_s"] = env_auth_wait
    if args.scale_in is not None:
        overrides["scale_in"] = args.scale_in
    if args.report_every:
        overrides["report_every"] = args.report_every
    if args.no_fat_tails:
        overrides["use_fat_tails"] = False
    if args.symbols:
        overrides["symbols"] = _parse_symbols_arg(args.symbols)
    elif env_symbols:
        overrides["symbols"] = _parse_symbols_arg(env_symbols)
        LOG.info("Markets pinned to %s by SYMBOLS",
                 ", ".join(overrides["symbols"]) or "(empty -> every market)")
    if args.max_concurrent is not None:
        overrides["max_concurrent_positions"] = args.max_concurrent

    try:
        config_path = args.config if os.path.exists(args.config) else None
        if config_path is None and args.config != "config.json":
            print(f"Config file {args.config} not found.", file=sys.stderr)
            return 1
        store = ConfigStore(
            None if args.no_hot_reload and config_path is None else config_path,
            api_key=key, api_secret=secret, live=live, db_path=args.db,
            profile=args.profile, overrides=overrides)
        cfg = store.current
        if config_path:
            LOG.info("Config from %s%s", config_path,
                     "" if not args.no_hot_reload else " (hot reload off)")
        else:
            LOG.info("No config file; using built-in defaults. "
                     "Run --write-config to create one.")
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1

    # Every command is wrapped: an uncaught exception reaching the user as a
    # traceback is a defect in its own right, regardless of the cause.
    commands = {
        "preflight": (args.preflight, preflight),
        "discover-min": (args.discover_min, discover_min),
        "whoami": (args.whoami, whoami),
    }
    for name, (selected, fn) in commands.items():
        if not selected:
            continue
        try:
            return fn(cfg)
        except ApiError as exc:
            print(f"\n{name} failed [{exc.kind.value}]: {exc}", file=sys.stderr)
            if exc.kind is ErrorKind.AUTH:
                print("  Check the API key's Wallet permissions and IP "
                      "allowlist.", file=sys.stderr)
            elif exc.kind is ErrorKind.INSUFFICIENT_FUNDS:
                print("  The account balance cannot cover the requested size.",
                      file=sys.stderr)
            elif exc.kind is ErrorKind.TIMING:
                print("  System clock drift; re-sync and retry.",
                      file=sys.stderr)
            elif exc.kind is ErrorKind.GEO_BLOCKED:
                print("  HTTP 451: Binance blocks this server's region. If you "
                      "are hosting,\n  redeploy to a non-US region "
                      "(e.g. Frankfurt or Singapore).", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130

    if cfg.live:
        print("\n*** LIVE MODE: this will spend real USDT. ***")
        print("Confirm you have (1) run --preflight clean, and (2) reviewed")
        print("--calibration-report over several hundred paper rounds.")
        # if input('Type "I ACCEPT THE RISK" to continue: ') != "I ACCEPT THE RISK":
        #     print("Aborted.")
        #     return 1

    try:
        Trader(store if not args.no_hot_reload else cfg).run()
    except ApiError as exc:
        print(f"\nStopped [{exc.kind.value}]: {exc}", file=sys.stderr)
        return 1
    except Exception:                        # noqa: BLE001
        # run() has already logged the traceback and drained. What is left is
        # to exit non-zero with a pointer to the journal, rather than dumping
        # a second copy of the same traceback onto the operator.
        print("\nStopped by an unexpected error; see the log above.",
              file=sys.stderr)
        print("  Open positions were drained before exit. Check the journal "
              "with --calibration-report before restarting.", file=sys.stderr)
        return 1
    return 0
