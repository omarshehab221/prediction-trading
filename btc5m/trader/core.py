"""
The trading loop: what it owns, how it starts, how it switches mode
and how it stops.
"""

from __future__ import annotations

import queue
import signal
import sqlite3
import threading
import time

import requests

import ws_feeds
from btc5m.config import Config
from btc5m.constants import LOG
from btc5m.errors import ApiError, Shutdown, TradingHalted
from btc5m.journal import Journal
from btc5m.paper import PaperBook
from btc5m.risk import RiskManager
from btc5m.trader.accounting import AccountingMixin
from btc5m.trader.bookkeeping import BookkeepingMixin
from btc5m.trader.claims import ClaimsMixin
from btc5m.trader.exits import ExitsMixin
from btc5m.trader.hybrid import HybridMixin
from btc5m.trader.last_minute import LastMinuteMixin
from btc5m.trader.model_entry import ModelEntryMixin
from btc5m.trader.order_lifecycle import OrderLifecycleMixin
from btc5m.trader.scale_in import ScaleInMixin
from btc5m.trader.scalp import ScalpMixin
from btc5m.trader.settling import SettlementMixin
from btc5m.trader.straddle import StraddleMixin
from btc5m.venue.client import PredictionClient
from btc5m.venue.shadow import ShadowClient
from btc5m.volatility import VolatilityEstimator

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config_file import ConfigStore
    from btc5m.domain import Bracket, PendingOrder, Position, Round, Side

class Trader(
        AccountingMixin,
        ClaimsMixin,
        BookkeepingMixin,
        OrderLifecycleMixin,
        ExitsMixin,
        SettlementMixin,
        ScaleInMixin,
        HybridMixin,
        StraddleMixin,
        ModelEntryMixin,
        LastMinuteMixin,
        ScalpMixin):
    """
    The trading loop.

    Installs a SIGTERM handler because hosted platforms (Render, Fly, Heroku)
    send SIGTERM on every deploy and restart, and Python does NOT turn that
    into KeyboardInterrupt -- the process simply dies. Without this an open
    position is abandoned mid-round and left unresolved in the journal, which
    silently corrupts the calibration record on every redeploy.
    """

    def __init__(self, cfg: Config | ConfigStore) -> None:
        self._store: ConfigStore | None = None
        self._static_cfg: Config | None = None
        if isinstance(cfg, Config):
            self._static_cfg = cfg
        else:
            self._store = cfg
        # Resolve once for construction. A ConfigStore has no config fields of
        # its own, so reading cfg.db_path off the argument would fail; every
        # later read goes through the _cfg property and sees reloads.
        config = self._resolve(cfg)
        # Shadow runs the live path with its writes simulated, so the choice
        # is made once, here, and every live branch below stays the live one.
        client_type = (ShadowClient if config.live and config.shadow
                       else PredictionClient)
        self._client = client_type(cfg)
        self._market_data = ws_feeds.MarketData(
            self._client, self._store or self._static_cfg)
        self._vol = VolatilityEstimator(cfg, self._market_data)
        # One seam for orders, so paper and live share the whole lifecycle
        # rather than paper taking a shortcut through it.
        self._paper_book = PaperBook(self._market_data)
        self._journal = Journal(config.db_path, config.profile_name)
        self._paper_bankroll = config.paper_start_bankroll
        # Per-symbol so one market's losing streak cannot gate another's
        # trading. The bankroll and the daily loss limit stay shared, because
        # there is only one account.
        self._risk: dict[str, RiskManager] = {}
        self._account_risk: RiskManager | None = None
        self._seen: dict[int, int] = {}
        # topic_id -> (end_ms, latest reason this round is not tradable).
        # A live round sits here and is re-examined on every poll; once it
        # expires the reason is tallied and the entry dropped.
        self._watching: dict[int, tuple[int, str]] = {}
        self._missed: dict[str, int] = {}
        self._missed_total = 0
        # Last "why am I not trading" message, so it is logged on change
        # rather than on every poll.
        self._idle_reason = ""
        # Keyed by (symbol, side) rather than just symbol: every model-based
        # profile only ever opens one side per symbol, so this changes
        # nothing for them, but it lets the straddle profile hold BOTH
        # sides of the same symbol/round as two independent entries.
        self._positions: dict[tuple[str, Side], Position] = {}
        # order_id -> the resting order behind it. Separate from _positions
        # because an order is not a position until something fills: counting
        # one as the other books a trade that may never happen, and not
        # tracking it at all abandons one that did.
        self._pending: dict[str, PendingOrder] = {}
        # Scalp state, keyed to match _positions and _seen so one prune pass
        # can clear all of it together.
        #
        # _scalp_entries counts round TRIPS per round rather than marking the
        # round seen: every other strategy enters a round once and writes
        # _seen, and doing that here would cap this profile at one trade in
        # the five minutes it exists to trade repeatedly.
        self._brackets: dict[tuple[str, Side], Bracket] = {}
        # topic_id -> (end_ms, entries taken on this round).
        self._scalp_entries: dict[int, tuple[int, int]] = {}
        self._flattened: set[int] = set()
        # symbol -> monotonic seconds of the last entry, for the cooldown.
        self._scalp_last_entry: dict[str, float] = {}
        # symbol -> (EWMA of the perp/spot basis in bps, samples seen).
        self._basis_ewma: dict[str, tuple[float, int]] = {}
        # symbol -> (reason, detail) for the last signal that was NOT there,
        # so the round that declines on it can say what the feed showed.
        self._signal_why: dict[str, tuple[str, str]] = {}
        # key -> (reason, monotonic seconds it was last logged). What the bot
        # is waiting on right now; see _explain for when it is repeated.
        self._explained: dict[str, tuple[str, float]] = {}
        self._hydrated: dict[int, Round] = {}
        # Symbols of the rounds the last pass actually saw. What the feeds
        # subscribe to when no symbols are configured; see _list_rounds.
        self._in_play: set[str] = set()
        self._errors = 0
        # token_id -> (expected payout USDT, tx hashes). Counted toward the
        # bankroll so an unclaimed win is not misread as a drawdown.
        self._unredeemed: dict[str, tuple[float, list[str], str]] = {}
        # A claim is handed to a background worker (see _claim) so that
        # settling a win never makes the trading loop wait on a chain
        # confirmation before it looks at the next round. The lock guards
        # every mutation of _unredeemed, since it is now written from two
        # threads; the worker itself is started lazily, on the first win.
        self._claim_lock = threading.Lock()
        self._claim_queue: "queue.Queue[Position]" = queue.Queue()
        self._claim_thread: threading.Thread | None = None
        self._stopping = False
        self._settled_count = 0
        # The mode actually in force. Diverges from the config only while a
        # position is open and a switch is pending.
        self._active_live = config.live

    @staticmethod
    def _resolve(cfg: Config | ConfigStore) -> Config:
        return cfg if isinstance(cfg, Config) else cfg.current

    @property
    def _cfg(self) -> Config:
        return self._static_cfg if self._store is None else self._store.current

    @property
    def _orders(self):
        """The venue in live mode, the simulator in paper mode."""
        return self._client if self._live else self._paper_book

    @property
    def _mode_label(self) -> str:
        """LIVE, SHADOW or PAPER -- what the journal and the log call a trade."""
        if not self._live:
            return "PAPER"
        return "SHADOW" if isinstance(self._client, ShadowClient) else "LIVE"

    @property
    def _position(self) -> Position | None:
        """The single open position, when exactly one market is configured."""
        if len(self._positions) == 1:
            return next(iter(self._positions.values()))
        return None

    @_position.setter
    def _position(self, value: Position | None) -> None:
        if value is None:
            self._positions.clear()
        else:
            self._positions[(value.rnd.symbol, value.signal.side)] = value

    @property
    def _live(self) -> bool:
        """
        The mode in force right now.

        Reads the ACTIVE mode rather than the configured one, so a pending
        switch cannot take effect halfway through a round. Every decision that
        turns on paper-vs-live goes through here.
        """
        return self._active_live

    def _apply_pending_mode(self) -> None:
        """Adopt a configured mode change, but only while flat."""
        wanted = self._cfg.live
        if wanted == self._active_live:
            return
        if self._positions or self._unredeemed:
            LOG.info("Mode change to %s is pending: waiting until flat "
                     "(open position or unclaimed winnings)",
                     "LIVE" if wanted else "PAPER")
            return

        self._active_live = wanted
        # The bankroll means something different in each mode, so the risk
        # baseline and streak counters would otherwise carry across a switch
        # and misreport the first day in the new mode.
        try:
            bankroll = self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Switched to %s but could not read the balance: %s",
                      "LIVE" if wanted else "PAPER", exc)
            return
        self._risk = {}
        self._account_risk = RiskManager(self._store or self._cfg, bankroll)
        LOG.warning("MODE NOW %s -- bankroll %.2f, risk counters reset",
                    "LIVE" if wanted else "PAPER", bankroll)

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            name = signal.Signals(signum).name
            if self._stopping:
                LOG.warning("%s again; exiting immediately", name)
                raise SystemExit(1)
            self._stopping = True
            LOG.info("%s received; finishing the open position then stopping",
                     name)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not the main thread, or the platform lacks the signal.
                LOG.debug("Could not install a handler for %s", sig)

    def run(self) -> None:
        self._install_signal_handlers()
        try:
            self._client.sync_clock()
            self._market_data.start()
            feeds = self._market_data.status()
            LOG.info("Feeds: spot %s, book %s, futures %s", feeds["spot"],
                     feeds["book"], feeds["futures"])
            if self._live:
                w = self._client.wallet()
                LOG.info("Prediction wallet %s", w.address)
                try:
                    quota = self._client.remaining_quota_usdt()
                    if quota is not None:
                        LOG.info("Remaining daily quota: %.2f USDT", quota)
                except ApiError as exc:
                    LOG.warning("Could not read daily quota: %s", exc)
            bankroll = self._bankroll()
        except (ApiError, requests.RequestException) as exc:
            LOG.error("Startup failed: %s", exc)
            LOG.error("Run --preflight to diagnose.")
            return

        self._account_risk = RiskManager(self._store or self._cfg, bankroll)
        LOG.info("Starting %s mode. Bankroll %.2f USDT. Markets: %s",
                 self._mode_label,
                 bankroll, ", ".join(self._cfg.symbols) or
                 "every 5m up/down market (auto-discovered)")
        last_sync = time.time()

        try:
            while True:
                if self._stopping:
                    raise Shutdown("stop signal received")
                try:
                    if self._store is not None and self._store.maybe_reload():
                        # Parsing reads class attributes, so refresh them
                        # whenever the configuration swaps.
                        PredictionClient.apply_config(self._cfg)
                    if time.time() - last_sync > self._cfg.clock_resync_s:
                        self._client.sync_clock()
                        last_sync = time.time()

                    # Redemption is no longer polled from here: a background
                    # worker (started in _claim, on the first win) chases
                    # every claim to confirmation on its own tight schedule,
                    # specifically so this loop never pauses on a chain
                    # confirmation before looking at the next round.
                    # Before settlement, always. A fill has to become a
                    # position before its round is allowed to settle, or the
                    # position settles as though it had never been opened.
                    self._reap_pending()
                    # After the reaper, so a take-profit that filled this
                    # pass has already closed its position and disarmed its
                    # own stop; before the flatten, so a stop that is due
                    # fires at its own price rather than at whatever the
                    # deadline can reach.
                    self._check_stops()
                    self._flatten_scalps()
                    self._maybe_exit_all()
                    self._settle_open()
                    # Driven from the loop, not from _maybe_enter: that
                    # returns early while a position is open, and the rounds
                    # passing unwatched during those minutes are exactly the
                    # ones worth counting.
                    self._tally_missed(self._client.now_ms())
                    bankroll = self._bankroll()
                    # Account-level limits: one balance, one daily loss cap.
                    # Per-market streaks are checked inside _maybe_enter.
                    self._account_risk.check(bankroll, self._outstanding())
                    self._apply_pending_mode()
                    self._maybe_scale_in_all(bankroll)
                    self._maybe_enter(bankroll,
                                      self._mode_label)
                    # Subscriptions follow the markets actually in
                    # play, and are set AFTER discovery rather than
                    # before it. Config.symbols defaults to empty --
                    # meaning trade everything the venue lists -- so
                    # the set is not knowable until the venue has
                    # been asked. It comes from the rounds this pass
                    # listed, never from open positions: a flat bot
                    # has none, and the scalp strategy cannot open one
                    # until this subscription has brought the futures
                    # socket up.
                    self._market_data.track(
                        self._cfg.symbols or self._in_play)
                    self._errors = 0
                except (TradingHalted, Shutdown):
                    raise
                # sqlite3.Error and OSError belong here for the same reason
                # ApiError does: a full disk, a briefly locked journal or a
                # socket the requests layer did not wrap are all transient
                # conditions that the backoff-then-halt path already handles
                # correctly. Left out, each one killed the process outright
                # while a position was open.
                except (ApiError, requests.RequestException,
                        sqlite3.Error, OSError) as exc:
                    self._errors += 1
                    LOG.warning("Recoverable error %d/%d: %s", self._errors,
                                self._cfg.max_consecutive_errors, exc)
                    if self._errors >= self._cfg.max_consecutive_errors:
                        raise TradingHalted(
                            "too many consecutive API errors; check "
                            "connectivity and endpoint paths") from exc
                    time.sleep(min(self._cfg.error_backoff_max_s,
                                   2.0 ** min(self._errors, 5)))

                time.sleep(self._cfg.poll_interval_s)

        except TradingHalted as exc:
            LOG.error("HALTED: %s", exc)
            if self._positions:
                LOG.info("Waiting for %d open position(s) to resolve...",
                         len(self._positions))
                self._drain()
            LOG.error("Stopped. Review the journal before restarting.")
        except Shutdown:
            if self._positions:
                LOG.info("Draining %d open position(s) before exit...",
                         len(self._positions))
                self._drain()
            LOG.info("Clean shutdown; no position abandoned.")
        except KeyboardInterrupt:
            LOG.info("Interrupted; open position left in the journal.")
        except Exception:                    # noqa: BLE001 - see below
            # A bug is not a reason to walk away from money already staked.
            # Every unanticipated exception previously escaped run(), killed
            # the process and abandoned the open position: the journal row
            # stayed unresolved and, in live mode, a winning token went
            # unclaimed. Catching here costs nothing -- the bot still stops,
            # and still stops loudly -- but it stops AFTER the position has
            # been settled and claimed.
            #
            # Deliberately not catching BaseException: SystemExit from the
            # second interrupt signal means "leave now", and KeyboardInterrupt
            # is handled above.
            LOG.exception("Unexpected error; shutting down")
            if self._positions:
                LOG.info("Draining %d open position(s) before exit...",
                         len(self._positions))
                try:
                    self._drain()
                except Exception:            # noqa: BLE001
                    LOG.exception("Drain failed; positions remain open")
            raise

    def _list_rounds(self):
        """
        The venue's live rounds, remembering which markets they were.

        Every entry strategy asks through here rather than calling the client
        directly, because the answer is also the subscription set: with no
        symbols configured, these are the only markets known to be in play.
        Taken from open positions instead, a flat bot subscribed to nothing,
        and the scalp strategy -- whose signal exists only on the futures
        socket -- could never open the position that would have subscribed
        it.
        """
        rounds = self._client.list_rounds()
        self._in_play = {r.symbol for r in rounds}
        return rounds

    def _maybe_enter(self, bankroll: float, mode: str) -> None:
        """Dispatch to whichever entry strategy the config selects."""
        if self._cfg.hybrid:
            self._maybe_enter_hybrid(bankroll, mode)
        elif self._cfg.straddle:
            self._maybe_enter_straddle(bankroll, mode)
        elif self._cfg.last_minute:
            self._maybe_enter_last_minute(bankroll, mode)
        elif self._cfg.scalp:
            self._maybe_enter_scalp(bankroll, mode)
        else:
            self._maybe_enter_model(bankroll, mode)


    def _drain(self, timeout_s: float | None = None) -> None:
        # Cancel first, then wait. A resting order left behind on shutdown is
        # live money with nothing tracking it: the process that placed it is
        # gone, so nothing will settle it, claim it, or even record that it
        # exists. Render sends SIGTERM on every deploy, so this is the
        # ordinary path and not the exceptional one.
        self._cancel_all_pending()
        deadline = time.time() + (timeout_s if timeout_s is not None
                                  else self._cfg.drain_timeout_s)
        while self._positions and time.time() < deadline:
            try:
                self._settle_open()
            except (ApiError, requests.RequestException) as exc:
                LOG.warning("Settle retry: %s", exc)
            time.sleep(self._cfg.drain_poll_s)
