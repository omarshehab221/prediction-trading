"""
Constants the whole bot shares: the logger, the float tolerance, the
round length, the fee fallback and the two socket tolerances.
"""

from __future__ import annotations

import logging

LOG = logging.getLogger("btc5m")


# Target round length. A constant here would silently exclude every market if
# the venue ever lists a different cadence; the tolerance is a fraction of the
# target rather than a fixed number of seconds.
DEFAULT_ROUND_SECONDS = 300


# Tolerance for float comparisons on money and probabilities. One name so a
# later change cannot leave some comparisons stricter than others.
EPS = 1e-9


# Fallback only, for journal rows written before the fee column existed.
DEFAULT_FEE_BPS = 200


# The perp/spot basis is tracked as an EWMA so the scalp strategy can measure
# a DISLOCATION rather than a level -- the level is mostly funding, which is a
# bias and not information. Not configurable on purpose: these describe how a
# mean is estimated, not what the strategy will accept, and every knob that
# blurs that distinction ends up tuned against the sample it was measured on.
#
# 0.05 is a ~14-sample half-life, which at the profile's 1s poll is about a
# quarter minute of "recent". The minimum sample count is what stops the very
# first observation being compared against itself and reported as a signal.
BASIS_EWMA_ALPHA = 0.05


BASIS_EWMA_MIN_SAMPLES = 20


# How far the WebSocket-derived ask ladder may sit from the REST ladder it is
# validated against, at top of book, in absolute price. Loose enough to
# survive the few hundred milliseconds between the push and the REST reply on
# a live book; far tighter than the |1 - 2p| error a transposed side mapping
# would produce anywhere away from 0.50, which is the mistake this check
# exists to catch.
WS_BOOK_VALIDATE_TOL = 0.02
