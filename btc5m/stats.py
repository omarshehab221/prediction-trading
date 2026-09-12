"""Distribution functions: the normal CDF, and Student-t for fat tails."""

from __future__ import annotations

import math

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for mth in range(1, 301):
        m2 = 2 * mth
        aa = mth * (b - mth) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + mth) * (qab + mth) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b). Used for the Student-t CDF."""
    if not 0.0 <= x <= 1.0:
        raise ValueError("x must be in [0, 1]")
    if x in (0.0, 1.0):
        return x
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(x: float, df: float) -> float:
    """
    CDF of the Student-t distribution with `df` degrees of freedom.

    Implemented in stdlib to keep the runtime dependency to `requests` alone;
    verified against scipy.stats.t in the test suite.
    """
    if df <= 0:
        raise ValueError("df must be positive")
    xt = df / (df + x * x)
    tail = 0.5 * betainc(df / 2.0, 0.5, xt)
    return 1.0 - tail if x > 0 else tail


def standardised_t_cdf(z: float, df: float) -> float:
    """
    Student-t CDF rescaled to unit variance, so `z` stays comparable to a
    Gaussian z-score.

    A raw t has variance df/(df-2); without this rescaling, switching to
    fat tails would silently change the volatility as well as the shape.
    """
    if df <= 2.0:
        raise ValueError("df must exceed 2 for finite variance")
    return student_t_cdf(z * math.sqrt(df / (df - 2.0)), df)
