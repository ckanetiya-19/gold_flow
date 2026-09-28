"""
Kyle's lambda -- price impact per unit of signed volume.

    dP_t = lambda * S_t + e_t          S_t = signed volume over the window

Two uses, both load-bearing:

  1. Position sizing. lambda is the cost, in dollars per contract, of the size
     you are about to send. A signal with a 3-tick expected move and a 4-tick
     impact-plus-spread cost is a losing strategy with a pretty backtest.

  2. As a liquidity STATE variable. Rising lambda means the book is thinning.
     It usually rises before a move rather than after it, which makes it a
     better regime input than realised volatility -- which by construction
     tells you about the move you already missed.

lambda is strongly session-dependent for gold: overnight lambda is a multiple
of New York lambda. Estimate it per session bucket, never once globally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..feeds.base import Side, Trade


@dataclass
class KyleFit:
    lam: float                 # price change per signed contract
    r2: float
    n: int
    t_stat: float
    window_ns: int

    def impact(self, size: int) -> float:
        """Expected adverse price move, in price units, for `size` contracts."""
        return self.lam * abs(size)

    def __str__(self) -> str:
        return (f"Kyle lambda={self.lam:.3e} price/contract  "
                f"R2={self.r2:.4f}  t={self.t_stat:+.1f}  n={self.n}")


def signed_volume_buckets(trades: Sequence[Trade], window_ns: int
                          ) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate a classified trade stream into (signed_volume, price_change)."""
    if not trades:
        return np.zeros(0), np.zeros(0)

    start = trades[0].ts_recv
    buckets: dict[int, list] = {}
    for t in trades:
        k = (t.ts_recv - start) // window_ns
        b = buckets.setdefault(k, [0.0, t.price, t.price])
        if t.side != Side.UNKNOWN:
            b[0] += int(t.side) * t.size
        b[2] = t.price

    keys = sorted(buckets)
    sv = np.array([buckets[k][0] for k in keys])
    dp = np.array([buckets[k][2] - buckets[k][1] for k in keys])
    return sv, dp


def fit_kyle(trades: Sequence[Trade], window_ns: int) -> KyleFit:
    sv, dp = signed_volume_buckets(trades, window_ns)
    if sv.size < 30:
        raise ValueError(f"need >=30 buckets, got {sv.size}")

    sxx = float(sv @ sv)
    if sxx <= 0:
        return KyleFit(0.0, 0.0, sv.size, 0.0, window_ns)
    lam = float(sv @ dp) / sxx
    resid = dp - lam * sv
    ss_res = float(resid @ resid)
    ss_tot = float(dp @ dp)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    se = np.sqrt(ss_res / max(sv.size - 1, 1) / sxx)
    t = lam / se if se > 0 else 0.0
    return KyleFit(lam, r2, int(sv.size), float(t), window_ns)


def rolling_lambda(trades: Sequence[Trade], window_ns: int,
                   n_buckets: int = 120, step: int = 20) -> tuple[np.ndarray, np.ndarray]:
    """lambda through time. Returns (bucket_index, lambda).

    Use the level relative to its own history, not the absolute number. A
    doubling of lambda is the signal; its units are contract-specific.
    """
    sv, dp = signed_volume_buckets(trades, window_ns)
    if sv.size < n_buckets + step:
        return np.zeros(0), np.zeros(0)

    idx, lams = [], []
    for i in range(n_buckets, sv.size, step):
        x = sv[i - n_buckets:i]
        y = dp[i - n_buckets:i]
        sxx = float(x @ x)
        if sxx <= 0:
            continue
        idx.append(i)
        lams.append(float(x @ y) / sxx)
    return np.asarray(idx), np.asarray(lams)
