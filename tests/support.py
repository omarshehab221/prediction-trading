"""
What the tests build with: configs, rounds, signals, a trader wired
to a fake client, and the fake clients themselves.
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import types

import btc_5m_predictor as m
from btc_5m_predictor import (
    Config,
    Journal,
    Position,
    PredictionClient,
    RiskManager,
    Round,
    Side,
    Signal,
    Trader,
)

# One directory up: this file lives in tests/, the project does not.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_client(config=None, **attrs):
    """
    Construct a PredictionClient without __init__ (which opens a session).

    `_cfg` is a read-only property that resolves through a ConfigStore, so
    tests set the backing field rather than the property. Centralised here so
    a change to how config is held does not have to be applied at 30 sites.
    """
    c = PredictionClient.__new__(PredictionClient)
    c._store = None
    c._store = None

    c._static_cfg = config if config is not None else cfg()
    c._clock_offset_ms = 0
    c._symbol_cache = {}
    c._wallet = None
    for key, value in attrs.items():
        setattr(c, key, value)
    return c


# Empty-container constructors build_trader's reflection must recognise,
# because ast.literal_eval refuses a call however harmless it is.
_EMPTY_CONTAINERS = {"set()": set, "dict()": dict, "list()": list}


def build_trader(client, config, db_path):
    """
    Construct a Trader without running __init__ (which does network I/O).

    Mirrors __init__ by reflection rather than by a hand-copied attribute
    list: five helpers each duplicated that list, so adding one field to
    Trader broke eleven tests at once. Anything __init__ sets that is not
    supplied here is initialised to a matching empty value.
    """
    t = Trader.__new__(Trader)
    t._store = None

    t._static_cfg = config
    t._client = client
    t._vol = types.SimpleNamespace(sigma_annual=lambda *a: 0.5,
                                   tail_df=lambda *a: None,
                                   is_clamped=lambda *a: False,
                                   raw_sigma=lambda *a: 0.5,
                                   trend=lambda *a: m.Trend())
    t._market_data = types.SimpleNamespace(
        spot=lambda symbol: client.spot_price(symbol),
        closes=lambda symbol: [],
        asks=lambda rnd, side: client.asks_for(rnd, side),
        bids=lambda rnd, side: getattr(client, "bids_for", lambda r, x: None)(
            rnd, side),
        # Silent by default, which is what an unsubscribed futures feed
        # answers -- so a test that does not set these up gets "no signal"
        # rather than an AttributeError from a strategy it is not testing.
        futures_mid=lambda symbol: None,
        futures_move_bps=lambda symbol, lookback_ms: None,
        futures_tick_age_ms=lambda symbol: None,
        track=lambda symbols: None,
        start=lambda: None,
        stop=lambda: None,
        status=lambda: {"spot": "off", "book": "off", "futures": "off"})
    t._journal = Journal(db_path, getattr(config, "profile_name", "test"))
    # The reflection below cannot evaluate PaperBook(self._market_data), so
    # without this every paper order path arrives at a None and fails deep
    # inside whatever placed it.
    t._paper_book = m.PaperBook(t._market_data)
    t._paper_bankroll = config.paper_start_bankroll
    t._risk = {}
    t._account_risk = RiskManager(config, config.paper_start_bankroll)
    t._positions = {}
    t._claim_lock = threading.Lock()
    t._claim_queue = queue.Queue()
    t._claim_thread = None
    # The mode actually in force. Derived from config, not reflected: the
    # reflection below cannot evaluate `config.live`, and defaulting it to
    # None made every live-mode test silently run as paper.
    t._active_live = config.live

    # Fill in everything else __init__ would have set.
    import ast as _ast, inspect as _inspect, textwrap as _tw
    src = _tw.dedent(_inspect.getsource(Trader.__init__))
    for node in _ast.walk(_ast.parse(src)):
        if not isinstance(node, (_ast.Assign, _ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
        for tgt in targets:
            if not (isinstance(tgt, _ast.Attribute)
                    and isinstance(tgt.value, _ast.Name)
                    and tgt.value.id == "self"):
                continue
            # Never assign through a read-only property, and never let a
            # failed hasattr abort the loop before later fields are set.
            if isinstance(getattr(type(t), tgt.attr, None), property):
                continue
            try:
                if hasattr(t, tgt.attr):
                    continue
            except AttributeError:
                pass
            expr = _ast.unparse(node.value) if node.value else "None"
            if expr.startswith("{"):
                setattr(t, tgt.attr, {})
            elif expr.startswith("["):
                setattr(t, tgt.attr, [])
            elif expr in ("False", "True"):
                setattr(t, tgt.attr, expr == "True")
            elif expr == "None":
                setattr(t, tgt.attr, None)
            elif expr in _EMPTY_CONTAINERS:
                # literal_eval cannot evaluate a call, so `set()` used to land
                # in the fallback below and arrive as None -- which does not
                # fail here, it fails much later at the first `|=` on it.
                setattr(t, tgt.attr, _EMPTY_CONTAINERS[expr]())
            else:
                try:
                    setattr(t, tgt.attr, _ast.literal_eval(expr))
                except (ValueError, SyntaxError):
                    setattr(t, tgt.attr, None)
    return t


def cfg(**kw) -> Config:
    """Balanced profile by default: most legacy tests assume a wide book."""
    base = dict(api_key="k", api_secret="s", live=False, **m.PROFILES["balanced"])
    base.update(kw)
    return Config(**base)


def convex_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", **m.PROFILES["convex"])
    base.update(kw)
    return Config(**base)


def straddle_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False, **m.PROFILES["straddle"])
    base.update(kw)
    return Config(**base)


def lastminute_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False,
                **m.PROFILES["lastminute"])
    base.update(kw)
    return Config(**base)


def scalp_cfg(**kw) -> Config:
    base = dict(api_key="k", api_secret="s", live=False,
                **m.PROFILES["scalp"])
    base.update(kw)
    return Config(**base)


def _close_journals(case=None) -> None:
    """
    Close every open Journal before a test unlinks its file.

    Windows refuses to unlink a file that still has an open handle, so a
    Journal left open turns every teardown into a PermissionError and buries
    whatever the test actually did under a cleanup error.

    It sweeps live instances rather than named attributes because a journal
    is reached three different ways across this suite -- as self.j, through a
    Trader, and as a local inside a helper that has already returned. Naming
    them would fix the first two and leave the third failing exactly as
    before.
    """
    import gc
    # Collect first. A Journal built as a temporary -- Journal(db).diagnose()
    # -- can still be sitting in an uncollected cycle, in which case the
    # sweep below finds nothing AND the connection has not been finalised,
    # which is the confusing case where closing everything visible still
    # leaves the file locked.
    gc.collect()
    for obj in gc.get_objects():
        if isinstance(obj, Journal):
            try:
                obj.close()
            except Exception:            # noqa: BLE001 - cleanup only
                pass


def _stage_bot(target_dir):
    """
    Copy the whole bot into a directory, the way a deploy would.

    Every root-level module, the package and the suite. Copying the entry point alone
    was enough while the entry point was the bot; it stopped being enough when
    ws_feeds arrived, and the entrypoint test has been failing on Linux ever
    since -- invisibly here, because Windows cannot exec the script at all.
    """
    import shutil as _sh, os as _os, glob as _glob
    for path in _glob.glob(_os.path.join(ROOT, "*.py")):
        _sh.copy(path, target_dir)
    for name in ("btc5m", "tests"):
        source = _os.path.join(ROOT, name)
        if _os.path.isdir(source):
            _sh.copytree(source, _os.path.join(target_dir, name),
                         ignore=_sh.ignore_patterns("__pycache__"),
                         dirs_exist_ok=True)


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


def make_signal(**kw) -> Signal:
    base = dict(side=Side.UP, model_prob=0.72, fill_price=0.55, edge=0.16,
                stake_usdt=5.0, seconds_left=60.0)
    base.update(kw)
    return Signal(**base)


def make_trader(**overrides):
    """A Trader wired to FakeClient, with no feeds and no network."""
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    base = dict(live=True, db_path=path, ws_enabled=False)
    base.update(overrides)
    c = cfg(**base)
    rnd = make_round()
    client = FakeClient([rnd], [(rnd.end_ms - 120_000, 100_000.0)], {}, {})
    client.cancel_orders = lambda ids: (list(ids), {})
    client.order_state = lambda oid: None
    t = build_trader(client, c, path)
    t._paper_book = m.PaperBook(t._market_data)
    return t


def make_pending(order_id, *, expires_in_ms, amount=5.0, round_ended=False,
                 action=None, price_limit=0.40, trade_id=None):
    rnd = make_round()
    now = rnd.end_ms if round_ended else rnd.end_ms - 120_000
    return m.PendingOrder(
        order_id=order_id, rnd=rnd,
        plan=m.OrderPlan(side=Side.UP, action=action or m.Action.BUY,
                         order_type=m.OrderType.LIMIT, amount=amount,
                         price_limit=price_limit),
        signal=make_signal(),
        expires_at_ms=now + expires_in_ms,
        filled_usdt=0.0, filled_shares=0.0, trade_id=trade_id)


def make_position(committed=5.0, trade_id=1):
    return Position(trade_id, make_round(), make_signal(), committed, 1)


def make_round(**kw) -> Round:
    base = dict(topic_id=1, market_id=9, vendor="PREDICT_FUN", slug="btc-5m",
                symbol="BTCUSDT",
                start_ms=1_700_000_000_000,
                end_ms=1_700_000_000_000 + (m.DEFAULT_ROUND_SECONDS * 1000),
                up_token_id="1", down_token_id="2",
                up_quote=0.50, down_quote=0.50, fee_bps=200,
                chain_id="56", collateral="USDT", venue_slippage_bps=1200,
                decimal_precision=4, liquidity=100_000.0,
                strike=100_000.0, feed_symbol="BTCUSDT")
    base.update(kw)
    return Round(**base)


class FakeClient:
    """Deterministic stand-in for PredictionClient. No network."""

    def __init__(self, rounds, spot_path, books, winners, balance=100.0):
        self._rounds = rounds
        self._spot_path = spot_path
        self._books = books
        self._winners = winners
        self.balance = balance
        self.orders = []
        self.redeemed = []
        self.redeem_fails = False
        self.redeem_state = "PENDING"
        self.t = 0

    def sync_clock(self):
        return 0

    def now_ms(self):
        return self._spot_path[min(self.t, len(self._spot_path) - 1)][0]

    def market_symbol(self, feed_symbol):
        return "BTCUSDT"

    def spot_price(self, symbol="BTCUSDT"):
        return self._spot_path[min(self.t, len(self._spot_path) - 1)][1]

    def hydrate(self, rnd):
        from dataclasses import replace as _r
        if rnd.strike is not None:
            return rnd
        return _r(rnd, strike=self._spot_path[0][1], feed_symbol="BTCUSDT")

    def settled_outcome(self, rnd):
        w = self._winners.get(rnd.topic_id)
        return None if w is None else (w, 0.0)

    def final_price(self, rnd):
        return self._spot_path[-1][1]

    def balance_usdt(self):
        return self.balance

    def get_quote(self, rnd, plan):
        return m.Quote("q1", 0.51, plan.amount / 0.51, 0.001, 0.0,
                       action=plan.action, order_type=plan.order_type,
                       price_limit=plan.price_limit)

    def batch_redeem(self, token_ids, chain_id="56"):
        self.redeemed.extend(token_ids)
        if self.redeem_fails:
            raise m.ApiError("redeem rejected")
        return ["0xtx" + t for t in token_ids]

    def redeem_status(self, tx_hash):
        return self.redeem_state

    def place_order(self, rnd, quote, stake_usdt=None):
        self.orders.append((quote.quote_id, quote.average_price, stake_usdt))
        return "order-1"

    def confirm_fill(self, order_id, requested_usdt):
        # Deterministic stand-in: every order fills in full. Tests that need
        # a partial or dead fill override this per-instance.
        return requested_usdt

    def list_rounds(self):
        return list(self._rounds)

    def asks_for(self, rnd, side):
        return self._books.get((rnd.topic_id, side))

    def resolved_winner(self, rnd):
        return self._winners.get(rnd.topic_id)

    def bankroll_usdt(self):
        return self.balance

    def place_market_buy(self, rnd, side, stake, max_price):
        self.orders.append((rnd.topic_id, side, stake, max_price))
        self.balance -= stake
        return min(max_price, 0.55)


class QuotingClient(FakeClient):
    """FakeClient whose quotes can differ per side, and can fail to place."""

    def __init__(self, *a, quotes=None, fail_order_after=None,
                 kill_fills=False, confirm_unreachable=False, **kw):
        super().__init__(*a, **kw)
        self._quotes = quotes or {}
        self._fail_after = fail_order_after
        self.kill_fills = kill_fills
        self.confirm_unreachable = confirm_unreachable

    def get_quote(self, rnd, plan):
        price = self._quotes.get(plan.side, 0.51)
        return m.Quote("q-" + plan.side.value, price, plan.amount / price,
                       0.001, 0.0, action=plan.action,
                       order_type=plan.order_type,
                       price_limit=plan.price_limit)

    def place_order(self, rnd, quote, stake_usdt=None):
        if (self._fail_after is not None
                and len(self.orders) >= self._fail_after):
            raise m.ApiError("venue rejected the order")
        return super().place_order(rnd, quote, stake_usdt)

    def confirm_fill(self, order_id, requested_usdt):
        if self.kill_fills:
            raise m.OrderNotFilled(f"order {order_id} did not fill: status "
                                   f"KILLED, filled 0.0")
        if self.confirm_unreachable:
            raise m.ApiError("read timed out")
        return super().confirm_fill(order_id, requested_usdt)


class ScalpClient(FakeClient):
    """
    FakeClient with a bid side and distinguishable order ids.

    The scalp path reads bids (the stop watches one, and a forced sale prices
    through one) and places several orders per round, so ids that all come
    back as "order-1" would make a resting take-profit and the sale that
    replaces it indistinguishable.
    """

    def __init__(self, *args, **kw):
        self.bids = kw.pop("bids", {})
        super().__init__(*args, **kw)
        self._next_id = 0
        self.cancelled = []
        self.states = {}

    def bids_for(self, rnd, side):
        return self.bids.get((rnd.topic_id, side))

    def place_order(self, rnd, quote, stake_usdt=None):
        self._next_id += 1
        order_id = f"o{self._next_id}"
        self.orders.append((order_id, quote.action, quote.average_price,
                            quote.price_limit, stake_usdt))
        return order_id

    def cancel_orders(self, order_ids):
        self.cancelled.extend(order_ids)
        return list(order_ids), {}

    def order_state(self, order_id):
        return self.states.get(order_id)


class _FakePosition:
    """Just enough of a Position for the loop's bookkeeping."""

    committed_usdt = 1.0
    signal = None
    rnd = None
