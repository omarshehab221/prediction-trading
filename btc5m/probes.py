"""
Operator probes: what the bot checks before it is allowed to trade,
and the two commands that answer 'who am I to the venue'.
"""

from __future__ import annotations

import time

import requests

import ws_feeds
from btc5m.constants import LOG
from btc5m.domain import Side, _market_buy
from btc5m.errors import ApiError, ErrorKind
from btc5m.pricing import breakeven_probability
from btc5m.sizing import kelly_multiple, kelly_stake
from btc5m.venue.client import PredictionClient
from btc5m.venue.endpoints import DEFAULT_ENDPOINTS
from btc5m.volatility import VolatilityEstimator

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.config import Config
    from btc5m.domain import Round

# Three services rather than one: any of them can be down, rate-limited or
# blocked, and "could not determine the IP" is a much worse answer here than
# a slightly slower one.
IP_SERVICES = ("https://api.ipify.org?format=json",
               "https://ifconfig.me/all.json",
               "https://ipinfo.io/json")


def outbound_ip(session: requests.Session,
                timeout: float = 8.0) -> str | None:
    """The address this process appears to come from, or None."""
    for url in IP_SERVICES:
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code != 200:
                continue
            body = response.json()
            found = body.get("ip") or body.get("ip_addr")
            if found:
                return str(found)
        except (requests.RequestException, ValueError) as exc:
            # Not silent: three services are tried precisely because any one
            # can be down, but "all three failed" is a real finding and has
            # to be visible under --verbose rather than reported as "no IP".
            LOG.debug("IP lookup via %s failed: %s", url, exc)
    return None


def wait_for_auth(cfg: Config, client: PredictionClient) -> bool:
    """
    Knock on a signed, IP-gated endpoint until Binance accepts us.

    WHY WAITING IS THE RIGHT ANSWER HERE
    ------------------------------------
    Binance's key allowlist accepts individual addresses. On shared egress
    the address is not known until the process is already running, and it can
    change on any restart -- so it cannot be added to the allowlist in
    advance. Failing on the first refusal means the deploy dies within a
    second of printing the one piece of information needed to fix it.

    Retrying converts that race into a window: the address is on screen, and
    the process keeps trying until the allowlist entry lands.

    Only AUTH refusals and network errors are retried. A 451 is the server's
    region, not its address, and no amount of waiting changes which continent
    the worker is on; anything else is a real fault and is reported at once
    rather than hidden behind several minutes of silence.
    """
    deadline = time.time() + cfg.auth_wait_timeout_s
    started = time.time()
    attempt = 0

    while True:
        attempt += 1
        try:
            client.sync_clock()
            client.wallet()
            if attempt > 1:
                print(f"  accepted after {time.time() - started:.0f}s "
                      f"({attempt} attempts).\n")
            return True
        except ApiError as exc:
            if exc.kind is ErrorKind.GEO_BLOCKED:
                print(f"  REFUSED [{exc.kind.value}]: {str(exc)[:100]}")
                print("  HTTP 451 is the server's REGION, not its address. "
                      "Waiting cannot fix it;\n  redeploy outside the US "
                      "(e.g. Frankfurt or Singapore).\n")
                return False
            if exc.kind is not ErrorKind.AUTH:
                print(f"  REFUSED [{exc.kind.value}]: {str(exc)[:100]}")
                print("  Not an allowlist problem, so waiting would only "
                      "delay the report.\n")
                return False
            reason = f"[{exc.kind.value}] {str(exc)[:80]}"
        except requests.RequestException as exc:
            reason = f"[network] {str(exc)[:80]}"

        remaining = deadline - time.time()
        if remaining <= 0:
            print(f"  still refused after {time.time() - started:.0f}s: "
                  f"{reason}")
            print("  -2015 covers the key, the allowlist and the key's "
                  "permissions together,\n  so check the Wallet permission "
                  "too before assuming it is the address.\n")
            return False
        print(f"  attempt {attempt}: {reason}  "
              f"(retrying, {remaining:.0f}s left)")
        time.sleep(min(cfg.auth_wait_poll_s, remaining))


def _probe_quote(client, rounds, first, stake: float) -> str:
    """
    Prove get-quote answers, without letting one thin book fail the boot.

    A quote refused for lack of liquidity is market state, not a broken
    connection: the request was signed, routed and understood. Live, preflight
    quoted only the first round's UP side, and "The market lacks enough
    liquidity" made the whole boot fatal and restart-looped the worker. So
    every live round and both sides are tried, and when all of them are thin
    the endpoint still counts as reachable. Any other refusal is raised.
    """
    refused = None
    for candidate in [first, *rounds]:
        for side in (Side.UP, Side.DOWN):
            try:
                quote = client.get_quote(candidate, _market_buy(side, stake))
            except ApiError as exc:
                if "liquidity" not in str(exc).lower():
                    raise
                refused = exc
                continue
            return f"avg {quote.average_price:.4f}"
    return f"reachable; every live book is too thin to quote right now ({refused})"


def preflight(cfg: Config) -> int:
    """Probe every endpoint and report which ones actually work."""
    client = PredictionClient(cfg)
    print("\n=== Preflight ===\n")
    failures = 0

    ip = outbound_ip(client.session)
    print(f"==> Outbound IP: {ip or 'could not be determined'}")
    if cfg.auth_wait_timeout_s > 0:
        print("    This exact address must be on the API key's allowlist -- "
              "not the CIDR\n    range a host's dashboard shows, which "
              "Binance cannot parse.")
        print(f"    Waiting up to {cfg.auth_wait_timeout_s:.0f}s for a signed "
              f"request to be accepted,\n    retrying every "
              f"{cfg.auth_wait_poll_s:.0f}s. Add it now.\n")
        if not wait_for_auth(cfg, client):
            failures += 1
    else:
        print("    Set auth_wait_timeout_s (or AUTH_WAIT_S) to have preflight "
              "wait here\n    while you add it to the allowlist.")
    print()

    def check(label, fn):
        nonlocal failures
        try:
            print(f"  {label:<24} OK   {fn()}")
        except Exception as exc:            # noqa: BLE001 - report everything
            failures += 1
            print(f"  {label:<24} FAIL {type(exc).__name__}: {str(exc)[:105]}")

    check("public spot", lambda: f"{cfg.symbol} {client.spot_price():,.2f}")
    check("clock sync", lambda: f"offset {client.sync_clock()} ms")

    md = ws_feeds.MarketData(client, cfg)
    md.start()
    # A moment for the handshakes. Not a health gate: REST-only is a
    # supported running mode, and PREFLIGHT_REQUIRED=1 must not start
    # refusing boots because an accelerator was slow.
    time.sleep(2.0)
    feeds = md.status()
    # futures is "off" unless the scalp strategy is selected -- and for that
    # strategy it is the one feed whose absence means no trading at all.
    check("websocket feeds",
          lambda: f"spot {feeds['spot']}, book {feeds['book']}, "
                  f"futures {feeds['futures']}")
    md.stop()
    def vol_check() -> str:
        est = VolatilityEstimator(cfg, ws_feeds.MarketData(client, cfg))
        sigma = est.sigma_annual()
        raw = est.raw_sigma()
        note = ""
        if est.is_clamped():
            note = (f"  <-- CLAMPED from a measured {raw:.4f}; trading refused "
                    f"while this holds")
        df = est.tail_df()
        df_s = "gaussian" if df is None else f"{df:.2f}"
        return (f"sigma {sigma:.4f} (measured {raw:.4f}) "
                f"tail_df {df_s}{note}")

    check("volatility", vol_check)
    check("wallet", lambda: client.wallet().address)
    def balance_check() -> str:
        options = client.payment_options()
        parts = [f"{t}={b:.2f}{'' if en else ' (disabled)'}"
                 for t, b, en in options]
        in_wallet = client.prediction_wallet_value()
        if in_wallet is not None:
            parts.append(f"PREDICTION_WALLET={in_wallet:.2f}")
        breakdown = ", ".join(parts) or "none"
        bal = client.balance_usdt()

        notes = [f"{bal:.2f} USDT  [{breakdown}]"]
        if bal < cfg.min_stake_usdt:
            notes.append(f"  <-- below the {cfg.min_stake_usdt:.2f} minimum "
                         f"order size; no order can be placed")
        elif cfg.scalp:
            # The scalp path sizes a fixed fraction floored at the venue
            # minimum and never calls kelly_stake, so the Kelly probe below
            # would call a tradeable account untradeable.
            per_trade = max(bal * cfg.scalp_stake_pct, cfg.min_stake_usdt)
            notes.append(f"  -> scalp stakes {per_trade:.2f} per trade "
                         f"({cfg.scalp_stake_pct:.0%} of bankroll, floored at "
                         f"the {cfg.min_stake_usdt:.2f} minimum)")
        elif cfg.last_minute:
            # The last-minute path never calls kelly_stake either, so probing
            # it would answer a question about a strategy this profile does
            # not run. Report the sizing that IS in force, and the two limits
            # that can silently zero it out.
            per_round = bal * cfg.last_minute_stake_pct
            spendable = bal * (1.0 - cfg.reserve_pct)
            notes.append(f"  -> last-minute stakes {per_round:.2f} per round; "
                         f"{spendable:.2f} spendable after the "
                         f"{cfg.reserve_pct:.0%} reserve")
            if per_round < cfg.min_stake_usdt:
                notes.append(
                    f"  <-- UNTRADEABLE: {per_round:.2f} per round is under "
                    f"the {cfg.min_stake_usdt:.2f} venue minimum, so no "
                    f"round will ever be entered. Raise "
                    f"last_minute_stake_pct (now "
                    f"{cfg.last_minute_stake_pct:.0%}) or add funds.")
            elif spendable < per_round:
                notes.append(
                    f"  <-- UNTRADEABLE: one round needs {per_round:.2f} but "
                    f"only {spendable:.2f} is spendable. Lower reserve_pct "
                    f"(now {cfg.reserve_pct:.0%}) or last_minute_stake_pct.")
        elif cfg.straddle:
            # The straddle path never calls kelly_stake, so probing it here
            # would answer a question about a strategy this profile does not
            # run. Report the sizing that IS in force -- and the two limits
            # that can silently zero it out, which is precisely what went
            # unnoticed until the bot sat idle in live mode.
            per_side = bal * cfg.straddle_stake_pct
            spendable = bal * (1.0 - cfg.reserve_pct)
            notes.append(f"  -> straddle stakes {per_side:.2f} per leg, "
                         f"{per_side * 2:.2f} per round; {spendable:.2f} "
                         f"spendable after the {cfg.reserve_pct:.0%} reserve")
            if per_side < cfg.min_stake_usdt:
                notes.append(
                    f"  <-- UNTRADEABLE: {per_side:.2f} per leg is under the "
                    f"{cfg.min_stake_usdt:.2f} venue minimum, so no round "
                    f"will ever be entered. Raise straddle_stake_pct "
                    f"(now {cfg.straddle_stake_pct:.0%}) or add funds.")
            elif spendable < per_side * 2:
                notes.append(
                    f"  <-- UNTRADEABLE: one round needs {per_side * 2:.2f} "
                    f"but only {spendable:.2f} is spendable. Lower "
                    f"reserve_pct (now {cfg.reserve_pct:.0%}) or "
                    f"straddle_stake_pct.")
        else:
            # Ask the real sizing function, not a percentage rule of thumb:
            # on a small balance the Kelly fraction binds long before the cap.
            # Probe at the midpoint of THIS profile's entry band. A fixed
            # 0.60 lies outside the buffer and convex bands entirely, so the
            # verdict described a trade those profiles would never make.
            probe = (cfg.min_entry_price + cfg.max_entry_price) / 2.0
            be = breakeven_probability(probe, cfg.fee_bps)
            strong = kelly_stake(bal, min(be + 0.10, 0.999), probe, cfg)
            weak = kelly_stake(bal, min(be + 0.01, 0.999), probe, cfg)
            if strong <= 0:
                forced = cfg.min_stake_usdt / bal
                notes.append(
                    f"  <-- UNTRADEABLE: even a strong edge sizes below the "
                    f"{cfg.min_stake_usdt:.2f} minimum, and the override is "
                    f"blocked ({forced:.0%} of bankroll exceeds the "
                    f"{cfg.hard_max_stake_pct:.0%} hard cap or 2x Kelly).")
            else:
                mult = kelly_multiple(strong, bal, min(be + 0.10, 0.999),
                                      probe, cfg.fee_bps)
                tag = f" at {mult:.2f}x full Kelly" if mult else ""
                notes.append(f"  -> strong edge stakes {strong:.2f}{tag}; "
                             f"marginal edge stakes {weak:.2f}")
        return "".join(notes)

    check("balance", balance_check)
    check("funding plan", lambda: (  # noqa: PLC3002
        lambda p: f"accountType={p[0]} fundingSource={p[1]} holder={p[2]}"
    )(client.funding_plan()))
    check("daily quota", lambda: f"{client.remaining_quota_usdt()}")

    rounds: list[Round] = []

    def list_rounds():
        rounds.extend(client.list_rounds())
        found = sorted({r.symbol for r in rounds})
        where = ", ".join(found) if found else "none"
        return f"{len(rounds)} live round(s): {where}"

    check("market list", list_rounds)

    if rounds:
        rnd = rounds[0]
        hydrated: list[Round] = []

        def hydrate():
            for candidate in rounds:
                h = client.hydrate(candidate)
                if h is not None:
                    hydrated.append(h)
                    return f"strike {h.strike:,.2f} feed {h.feed_symbol}"
            # A round publishes its strike when it opens, so a freshly listed
            # one legitimately has none -- failing the boot on that turned an
            # expected state into a restart loop whenever preflight happened
            # to run between listing and open. Prove the endpoint answers
            # instead: a real failure still raises and still fails the check.
            client.market_detail(rnd.topic_id)
            return "reachable; no live round has published its strike yet"

        check("market detail", hydrate)
        check("venue parameters", lambda: (
            f"fee {rnd.fee_bps}bps  chain {rnd.chain_id}  "
            f"collateral {rnd.collateral}  precision {rnd.decimal_precision}  "
            f"slippage venue={rnd.venue_slippage_bps} used="
            f"{client.effective_slippage_bps(rnd)}  "
            f"liquidity {rnd.liquidity:,.0f}  "
            f"duration {rnd.duration_ms/1000:.0f}s"))
        check("order book",
              lambda: f"{len(client.asks_for(rnd, Side.UP) or [])} ask levels")
        check("settled history",
              lambda: f"{client.settled_outcome(rnd)}")
        check("redeem status",
              lambda: f"{client.redeem_status('0x0') or 'reachable'}")
        if hydrated:
            check("quote (no order)", lambda: _probe_quote(
                client, rounds, hydrated[0], cfg.min_stake_usdt))
    else:
        print("  (no live rounds -- detail/book/quote checks skipped)")

    print()
    if failures:
        print(f"{failures} check(s) failed.\n"
              "Override paths in the config file under \"endpoints\":\n"
              '  {"order_book": "/sapi/v1/w3w/wallet/prediction/order-book"}\n'
              "Valid keys: " + ", ".join(sorted(DEFAULT_ENDPOINTS)))
        return 1
    print("All probed endpoints OK. place-order-bundle is NOT probed here --\n"
          "it would spend money. Confirm it with one minimum-size manual\n"
          "trade before --live.")
    return 0


def whoami(cfg: Config, samples: int = 8) -> int:
    """
    Report the outbound IP this process actually uses.

    Render's Connect menu shows shared CIDR *ranges*, while Binance's API-key
    allowlist accepts individual addresses only. Pasting one address out of a
    /24 matches only when that address happens to be the one used, which is
    why the failure looks intermittent. Sampling repeatedly makes the churn
    visible instead of guessed at.
    """
    import collections

    session = requests.Session()
    seen: collections.Counter = collections.Counter()

    print(f"\nSampling the outbound IP {samples} times...\n")
    for i in range(samples):
        got = outbound_ip(session)
        if got:
            seen[got] += 1
            print(f"  sample {i+1}: {got}")
        else:
            print(f"  sample {i+1}: could not determine")
        time.sleep(0.4)

    if not seen:
        print("\nNo IP could be determined. Outbound HTTP may be blocked.")
        print("  Tried: " + ", ".join(IP_SERVICES))
        print("  Re-run with --verbose to see why each one failed.")
        return 1

    print(f"\n  distinct addresses observed: {len(seen)}")
    for ip, count in seen.most_common():
        print(f"    {ip}  ({count}/{samples})")

    print("\n  What to do with this:")
    if len(seen) > 1:
        print("    The address CHANGED between requests. An allowlist keyed to")
        print("    any single one of these will fail intermittently. You need")
        print("    dedicated/static egress, or no IP restriction on the key.")
    else:
        print("    Stable across this sample -- but a shared range can still")
        print("    reassign it on the next deploy or restart. Stability over")
        print("    eight requests is not a guarantee across deploys.")
    print("    Paste the exact address(es) above into the Binance key's")
    print("    allowlist -- not the CIDR range from Render's Connect menu,")
    print("    which Binance cannot parse.")

    # Prove whether Binance itself accepts us, which is the real question.
    print("\n  Checking whether Binance accepts this source...")
    client = PredictionClient(cfg)
    try:
        client.sync_clock()
        client.wallet()
        print("    OK: a signed request succeeded from this IP.")
        return 0
    except ApiError as exc:
        print(f"    FAIL [{exc.kind.value}]: {exc}")
        if exc.kind is ErrorKind.AUTH:
            print("    -2015 covers key, IP and permissions together. If the")
            print("    address above is allowlisted, check the key's Wallet")
            print("    permission next -- the code does not distinguish them.")
        return 1


def discover_min(cfg: Config) -> int:
    """Measure the venue's real minimum order size instead of assuming it."""
    client = PredictionClient(cfg)
    client.sync_clock()
    rounds = client.list_rounds()
    if not rounds:
        print("No live rounds to probe.")
        return 1

    rnd = client.hydrate(rounds[0]) or rounds[0]
    print(f"\nProbing {rnd.slug} (fee {rnd.fee_bps} bps, "
          f"liquidity {rnd.liquidity:,.0f}, precision {rnd.decimal_precision}, "
          f"chain {rnd.chain_id}, collateral {rnd.collateral})")
    print("Quotes are non-binding; no order is placed.\n")

    # Show the raw outcome at each size before drawing any conclusion.
    print("  probe results:")
    sizes = [1.0, 1.5, 2.0, 5.0, 10.0, 25.0]
    verdicts: list[tuple[float, bool, str]] = []
    for amount in sizes:
        try:
            q = client.get_quote(rnd, _market_buy(Side.UP, amount))
            verdicts.append((amount, True, (f"avg {q.average_price:.4f} "
                                           f"impact {q.price_impact:.4f}")))
        except ApiError as exc:
            verdicts.append((amount, False, str(exc)[:130]))
    for amount, ok, detail in verdicts:
        print(f"    {amount:>6.2f} USDT  {'OK  ' if ok else 'FAIL'}  {detail}")

    if not any(ok for _, ok, _ in verdicts):
        print("\n  Every size failed, so this is NOT a minimum-size problem.")
        print("  The message above is the venue's own; read it literally.")
        print("  Common causes: the quote request is missing or malformed a")
        print("  parameter, the wallet is not authorised for trading, or the")
        print("  token id is not tradable in this round.")
        print("\n  Re-run with --verbose to see the full signed request.")
        return 1

    smallest_ok = next((a for a, ok, _ in verdicts if ok), None)

    try:
        found = client.discover_min_stake(rnd, Side.UP)
    except ApiError as exc:
        # Never discard what the probe table already established.
        print(f"\n  Refinement stopped [{exc.kind.value}]: {exc}")
        if smallest_ok is not None:
            print(f"  Probing already showed {smallest_ok:.2f} USDT quotes "
                  f"successfully, so the minimum is at or below that.")
            return 0
        return 1

    if found is None:
        if smallest_ok is not None:
            print(f"\n  Search did not converge, but {smallest_ok:.2f} USDT "
                  f"quoted successfully -- treat that as the practical "
                  f"minimum.")
            return 0
        print("\n  No tested size quoted successfully.")
        return 1

    print(f"  Smallest quotable amount: ~{found:.2f} USDT")
    print(f"  Configured min_stake_usdt: {cfg.min_stake_usdt:.2f} USDT")
    if found > cfg.min_stake_usdt:
        print("\n  Your configured minimum is BELOW what this market accepts.")
        print(f"  Orders would be rejected. Consider min_stake_usdt={found:.2f}.")
    else:
        print("\n  Configured minimum is acceptable for this market.")
    print("  Note: this varies by market depth, so it is a snapshot, not a"
          " constant.")
    return 0
