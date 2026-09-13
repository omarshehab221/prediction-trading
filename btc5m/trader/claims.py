"""
Claiming a win, on a worker thread, so settling one round never makes
the loop wait on a chain confirmation before it looks at the next.
"""

from __future__ import annotations

import threading
import time

import requests

from btc5m.constants import LOG
from btc5m.errors import ApiError, NothingToRedeem, _is_already_redeemed
from btc5m.venue.client import PredictionClient

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from btc5m.domain import Position


class ClaimsMixin:
    """The background redemption worker."""
    def _start_claim_worker(self) -> None:
        """Start the background redemption worker, once, on first use."""
        if self._claim_thread is not None and self._claim_thread.is_alive():
            return
        self._claim_thread = threading.Thread(
            target=self._claim_worker_loop, name="claim-worker", daemon=True)
        self._claim_thread.start()

    def _claim_worker_loop(self) -> None:
        """Consume winning positions and chase each redemption to landing."""
        while True:
            pos = self._claim_queue.get()
            try:
                self._claim_relentlessly(pos)
            except Exception:                # noqa: BLE001 - see below
                # A bug in the claim path must not silently strand a real
                # win: log it loudly and keep the worker alive for the next
                # one rather than letting an uncaught exception kill the
                # thread out from under the trading loop.
                LOG.exception("Claim worker error settling %s", pos.rnd.slug)
            finally:
                self._claim_queue.task_done()

    def _forget_claim(self, token_id: str) -> None:
        """
        Stop tracking a win the venue says is no longer redeemable.

        Almost always the operator claimed it by hand. The bot's only proof
        of a landed claim is the status of a tx hash it submitted itself, so
        a redemption performed anywhere else is invisible to it: without this
        the worker resubmits until claim_timeout_s and then keeps the token
        in _unredeemed for the life of the process, where it inflates
        _outstanding, blocks every reconciliation, and pins a pending mode
        switch on winnings that were credited long ago.

        Callers log the reason themselves. Which of the two ways the venue
        said "there is nothing here" is the only interesting part of this
        event, and burying it one frame deeper hid it from the reader and
        from the meta-test that insists a handler explain what it swallowed.
        """
        with self._claim_lock:
            self._unredeemed.pop(token_id, None)

    def _claim_relentlessly(self, pos: Position) -> None:
        """
        Redeem one winning position and keep trying until it is confirmed.

        This runs off the main thread specifically so the trading loop never
        waits on it: it can take anywhere from about a second to roughly a
        minute for a claim to land on chain, and every one of those seconds
        is a second the next round's open is unwatched. Retrying on a short,
        dedicated interval -- rather than once per poll_interval_s tick --
        is what gets the balance freed up for re-entry as early as possible.
        """
        token_id = pos.rnd.token_for(pos.signal.side)
        payout = pos.signal.stake_usdt / pos.signal.fill_price
        chain_id = pos.rnd.chain_id
        deadline = time.time() + self._cfg.claim_timeout_s
        hashes: list[str] = []
        # The venue's most recent answer, carried into the timeout warning.
        # Without it "not confirmed" reads the same whether the chain is
        # slow, the claim is refused, or the payout landed without one.
        last_answer = "no answer yet"
        refusal_warned = False

        while time.time() < deadline:
            if not hashes:
                try:
                    hashes = self._client.batch_redeem([token_id], chain_id)
                    with self._claim_lock:
                        self._unredeemed[token_id] = (payout, hashes,
                                                      chain_id)
                    LOG.info("Redeeming %.2f USDT (tx %s)", payout,
                             ", ".join(hashes) or "pending")
                except NothingToRedeem as exc:
                    LOG.warning("Nothing left to redeem for %s: treating "
                                "%.2f USDT as already credited (claimed "
                                "outside this bot?) -- %s",
                                pos.rnd.slug, payout, exc)
                    self._forget_claim(token_id)
                    return
                except (ApiError, requests.RequestException) as exc:
                    if _is_already_redeemed(exc):
                        LOG.warning("Venue refuses to redeem %s because it "
                                    "is already claimed: treating %.2f USDT "
                                    "as credited -- %s",
                                    pos.rnd.slug, payout, exc)
                        self._forget_claim(token_id)
                        return
                    last_answer = f"redeem refused: {exc}"
                    if not refusal_warned:
                        # Once, at WARNING: retries run every
                        # claim_poll_interval_s, and the same refusal on each
                        # of them would bury the log it is meant to explain.
                        LOG.warning("Redeem for %s refused, retrying for up "
                                    "to %.0fs: %s", pos.rnd.slug,
                                    self._cfg.claim_timeout_s, exc)
                        refusal_warned = True
                    else:
                        LOG.debug("Redeem attempt for %s failed, retrying: "
                                  "%s", token_id, exc)
                    time.sleep(self._cfg.claim_poll_interval_s)
                    continue

            if hashes:
                try:
                    statuses = [self._client.redeem_status(h)
                               for h in hashes]
                except (ApiError, requests.RequestException) as exc:
                    last_answer = f"status check failed: {exc}"
                    LOG.debug("Status check for %s failed, retrying: %s",
                              token_id, exc)
                    time.sleep(self._cfg.claim_poll_interval_s)
                    continue
                last_answer = "tx status " + (
                    ", ".join(s or "unknown" for s in statuses) or "unknown")
                if statuses and all(
                        s in ("SUCCESS", "CONFIRMED", "COMPLETED")
                        for s in statuses):
                    with self._claim_lock:
                        self._unredeemed.pop(token_id, None)
                    LOG.info("Redemption confirmed: %.2f USDT credited (%s)",
                             payout, pos.rnd.slug)
                    return
                if any(s in PredictionClient.DEAD_REDEEM_STATUSES
                       for s in statuses if s):
                    # The transaction is not coming back. Forget the hash and
                    # go round again: either the resubmission succeeds, or
                    # batch_redeem answers that there is nothing left to
                    # claim -- which is what a hand-redemption racing the
                    # bot's own transaction looks like from here.
                    LOG.warning("Redemption tx for %s reported %s; "
                                "resubmitting", pos.rnd.slug,
                                ", ".join(s or "?" for s in statuses))
                    hashes = []
                    with self._claim_lock:
                        self._unredeemed[token_id] = (payout, [], chain_id)

            time.sleep(self._cfg.claim_poll_interval_s)

        LOG.warning("Redemption for %s not confirmed within %.0fs (last "
                    "venue answer: %s); still tracked as unredeemed and will "
                    "keep being retried the next time this token is claimed",
                    pos.rnd.slug, self._cfg.claim_timeout_s, last_answer)

    def _claim(self, pos: Position) -> None:
        """
        Hand a winning position to the background claim worker.

        Returns immediately -- not waiting for the chain -- which is the
        entire point: the trading loop keeps scanning for the next round's
        open the instant this one settles, while the worker chases the
        actual redemption on its own schedule in the background.
        """
        token_id = pos.rnd.token_for(pos.signal.side)
        payout = pos.signal.stake_usdt / pos.signal.fill_price
        with self._claim_lock:
            self._unredeemed[token_id] = (payout, [], pos.rnd.chain_id)
        self._start_claim_worker()
        self._claim_queue.put(pos)
