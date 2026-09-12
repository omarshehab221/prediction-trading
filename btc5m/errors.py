"""
What went wrong, classified by the venue's own code rather than by
the wording of its message.
"""

from __future__ import annotations

from enum import Enum

class Shutdown(Exception):
    """A platform stop signal (SIGTERM) was received."""


class TradingHalted(Exception):
    """A risk limit tripped. Always fails closed."""


class ErrorKind(str, Enum):
    """
    What an API failure actually means.

    Classified from the venue's numeric code wherever possible. Matching on
    message substrings is fragile -- wording changes, locales differ, and a
    keyword list silently mis-files anything it does not recognise. The code
    is the structured field; the text is only a last resort.
    """

    SIZE = "SIZE"                    # order too small / not enough depth
    INSUFFICIENT_FUNDS = "FUNDS"     # balance cannot cover the order
    AUTH = "AUTH"                    # key, signature, permissions, IP
    TIMING = "TIMING"                # clock drift / recvWindow
    PARAMETER = "PARAMETER"          # malformed or missing parameter
    NOT_FOUND = "NOT_FOUND"
    GEO_BLOCKED = "GEO_BLOCKED"      # HTTP 451: server is in a restricted region
    UNKNOWN = "UNKNOWN"


# Venue codes observed or documented. Anything absent stays UNKNOWN, which
# callers must treat as "do not proceed" rather than "probably harmless".
ERROR_CODES: dict[int, ErrorKind] = {
    -9000: ErrorKind.INSUFFICIENT_FUNDS,
    -3026: ErrorKind.PARAMETER,
    -1022: ErrorKind.AUTH,
    -2014: ErrorKind.AUTH,
    -2015: ErrorKind.AUTH,
    -1002: ErrorKind.AUTH,
    -1021: ErrorKind.TIMING,
    -1102: ErrorKind.PARAMETER,
    -1104: ErrorKind.PARAMETER,
    -1121: ErrorKind.PARAMETER,
}


# Fallback only, when no numeric code is supplied.
_MESSAGE_HINTS: tuple[tuple[tuple[str, ...], ErrorKind], ...] = (
    (("enough", "insufficient balance", "insufficient funds"),
     ErrorKind.INSUFFICIENT_FUNDS),
    (("minimum", "too small", "min amount", "insufficient liquidity",
      "depth"), ErrorKind.SIZE),
    (("signature", "api-key", "api key", "permission", "unauthorized"),
     ErrorKind.AUTH),
    (("timestamp", "recvwindow"), ErrorKind.TIMING),
    (("mandatory parameter", "illegal characters", "not supported"),
     ErrorKind.PARAMETER),
)


class _AboveBalance(Exception):
    """Internal: a probe size exceeded the wallet balance, not the floor."""

    def __init__(self, amount: float) -> None:
        super().__init__(f"probe {amount} exceeds balance")
        self.amount = amount


class ApiError(RuntimeError):
    """
    Transient or structural API failure, carrying the venue's own code.

    The code travels with the exception so callers can branch on what went
    wrong instead of re-parsing the message.
    """

    def __init__(self, message: str, code: int | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def kind(self) -> ErrorKind:
        if self.status == 404:
            return ErrorKind.NOT_FOUND      # wrong path, or unknown resource
        if self.status == 451:
            # Binance refuses restricted locations, which includes the United
            # States. A US-region host will fail every call with this.
            return ErrorKind.GEO_BLOCKED
        if self.code is not None and self.code in ERROR_CODES:
            return ERROR_CODES[self.code]
        if self.code is not None:
            return ErrorKind.UNKNOWN     # a code we do not know: do not guess
        text = str(self).lower()
        for needles, kind in _MESSAGE_HINTS:
            if any(n in text for n in needles):
                return kind
        return ErrorKind.UNKNOWN


class OrderNotFilled(ApiError):
    """
    The venue confirmed an order did NOT fill -- killed, cancelled, rejected.

    Separate from ApiError because the two demand opposite responses. This
    one is a fact: no position exists, and recording one invents a trade that
    later "settles" and books a profit never made. A bare ApiError from the
    same call is an absence of information -- a timeout, a dropped socket --
    where a position may well exist, and dropping it strands real money that
    is never settled and never claimed. Catching both together forces one
    wrong answer or the other.
    """


class NothingToRedeem(ApiError):
    """
    The venue accepted the claim and found nothing to claim.

    Separate from ApiError for the same reason OrderNotFilled is: this one is
    a fact -- the tokens are gone, which on a winning position means the
    payout has already been credited, usually because the operator redeemed
    it by hand in the Binance app. A bare ApiError is an absence of
    information. Returning an empty hash list for both made them
    indistinguishable from a batch still in flight, so the claim worker
    re-submitted a redemption that could never succeed for the whole timeout
    and then held the token as unredeemed forever.
    """


# Message fragments that mean the same fact arrived as an error rather than
# an empty batch. Deliberately narrow: a match drops the bot's claim on real
# money, so anything vaguer than "there is nothing here to redeem" must fall
# through to the ordinary retry.
_ALREADY_REDEEMED_HINTS: tuple[str, ...] = (
    "already redeemed", "already been redeemed", "already claimed",
    "already been claimed", "no redeemable", "not redeemable",
    "nothing to redeem", "no position to redeem",
)


def _is_already_redeemed(exc: BaseException) -> bool:
    """Whether the venue's refusal says the tokens are already gone."""
    if not isinstance(exc, ApiError):
        return False        # a transport failure proves nothing either way
    text = str(exc).lower()
    return any(hint in text for hint in _ALREADY_REDEEMED_HINTS)
