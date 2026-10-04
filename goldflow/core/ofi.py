"""
Order Flow Imbalance -- Cont, Kukanov & Stoikov (2014).

The strongest-evidence object in this whole package. Reported average R^2 of
~65% regressing 10-second mid-price changes on OFI across 50 equities, with a
price-impact coefficient that scales as 1/depth (lambda_hat ~= 0.98).

Two implementation details that are easy to get wrong and that destroy the
result when wrong:

1. The event contribution e_n counts a CANCELLATION the same as a market order
   when both remove the same size from the same queue. That is deliberate --
   it is why OFI beats trade-only measures like delta. Do not "fix" it by
   filtering to trades.

2. Both indicators fire when the price is unchanged (P_n >= P_{n-1} AND
   P_n <= P_{n-1}), which correctly reduces to the size delta q_n - q_{n-1}.
   Writing this as an if/elif chain instead of two indicator terms is the
   classic bug: it silently drops every same-price size change, which is most
   of the informative events.

Practical consequence of lambda ~= 1: the SAME OFI moves a thin overnight book
several times further than a full New York book. Any fixed threshold fires
constantly at 02:00 and never at 09:30. Always normalise by contemporaneous
depth -- `OFIBucket.normalised` does this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

import numpy as np

from ..feeds.base import Quote


def event_contribution(prev: Quote, cur: Quote) -> float:
    """e_n for one book event. Positive = net buying pressure on the queue."""
    e = 0.0
    # bid side
    if cur.bid_px >= prev.bid_px:
        e += cur.bid_sz
    if cur.bid_px <= prev.bid_px:
        e -= prev.bid_sz
    # ask side
    if cur.ask_px <= prev.ask_px:
        e -= cur.ask_sz
    if cur.ask_px >= prev.ask_px:
        e += prev.ask_sz
    return float(e)


def ofi_series(quotes: Sequence[Quote]) -> np.ndarray:
    """e_n for a whole quote sequence. Length len(quotes) - 1."""
    n = len(quotes)
    if n < 2:
        return np.zeros(0)
    out = np.empty(n - 1)
    prev = quotes[0]
    for i in range(1, n):
        cur = quotes[i]
        out[i - 1] = event_contribution(prev, cur)
        prev = cur
    return out


@dataclass(slots=True)
class OFIBucket:
    """One aggregation interval."""

    ts_start: int
    ts_end: int
    ofi: float
    n_events: int
    depth_sum: float          # sum of (bid_sz + ask_sz) over events
    mid_start: float
    mid_end: float
    spread_mean: float

    @property
    def avg_depth(self) -> float:
        """AD_i from the paper: mean of (q_b + q_a) / 2 over interior events."""
        denom = 2.0 * max(self.n_events - 1, 1)
        return self.depth_sum / denom

    @property
    def d_mid(self) -> float:
        return self.mid_end - self.mid_start

    @property
    def normalised(self) -> float:
        """OFI / AD -- the depth-invariant form. Use THIS for thresholds."""
        ad = self.avg_depth
        return self.ofi / ad if ad > 0 else 0.0


class OFIBucketer:
    """Streaming aggregation of book events into fixed-time OFI buckets.

    Online: feed quotes as they arrive, get a bucket back whenever one closes.
    The same class is used in research (over a replay) and in the live engine,
    which is the only way to be sure the two agree.
    """

    def __init__(self, interval_ns: int, use_recv_time: bool = True):
        self.interval_ns = int(interval_ns)
        self.use_recv_time = use_recv_time
        self._prev: Quote | None = None
        self._bucket_start: int | None = None
        self._ofi = 0.0
        self._n = 0
        self._depth_sum = 0.0
        self._spread_sum = 0.0
        self._mid_start = float("nan")
        self._mid_last = float("nan")

    def _ts(self, q: Quote) -> int:
        return q.ts_recv if self.use_recv_time else q.ts_event

    def _reset(self, start: int, q: Quote) -> None:
        self._bucket_start = start
        self._ofi = 0.0
        self._n = 0
        self._depth_sum = 0.0
        self._spread_sum = 0.0
        self._mid_start = q.mid
        self._mid_last = q.mid

    def update(self, q: Quote) -> OFIBucket | None:
        """Feed one quote. Returns a closed bucket, or None."""
        if q.is_crossed():
            # A crossed book is a feed artefact (or a snapshot boundary).
            # Dropping it is correct; counting it corrupts e_n.
            return None

        ts = self._ts(q)
        if self._prev is None:
            self._prev = q
            self._reset(ts - (ts % self.interval_ns), q)
            return None

        out: OFIBucket | None = None
        edge = self._bucket_start + self.interval_ns
        if ts >= edge:
            if self._n > 0:
                out = OFIBucket(
                    ts_start=self._bucket_start,
                    ts_end=edge,
                    ofi=self._ofi,
                    n_events=self._n,
                    depth_sum=self._depth_sum,
                    mid_start=self._mid_start,
                    mid_end=self._mid_last,
                    spread_mean=self._spread_sum / self._n,
                )
            # skip empty intervals rather than emitting zero-OFI noise
            self._reset(ts - (ts % self.interval_ns), q)

        self._ofi += event_contribution(self._prev, q)
        self._n += 1
        self._depth_sum += q.bid_sz + q.ask_sz
        self._spread_sum += q.spread
        self._mid_last = q.mid
        self._prev = q
        return out

    def run(self, quotes: Iterable[Quote]) -> Iterator[OFIBucket]:
        for q in quotes:
            b = self.update(q)
            if b is not None:
                yield b


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

@dataclass
class OFIFit:
    beta: float
    r2: float
    n: int
    t_stat: float
    beta_normalised: float
    r2_normalised: float
    lambda_hat: float | None = None
    c_hat: float | None = None
    r2_depth: float | None = None
    # forward-looking regression: dP_{k+1} = beta * OFI_k
    beta_predictive: float = 0.0
    r2_predictive: float = 0.0
    t_predictive: float = 0.0

    def __str__(self) -> str:
        lam = "n/a" if self.lambda_hat is None else f"{self.lambda_hat:.3f}"
        r2d = "n/a" if self.r2_depth is None else f"{self.r2_depth:.3f}"
        return (
            f"OFI fit  n={self.n}\n"
            f"  contemporaneous  beta={self.beta:+.3e}  R2={self.r2:.4f}  t={self.t_stat:+.1f}\n"
            f"  depth-normalised beta={self.beta_normalised:+.3e}  R2={self.r2_normalised:.4f}\n"
            f"  beta ~ c/AD^lambda : lambda={lam}  R2={r2d}\n"
            f"  PREDICTIVE       beta={self.beta_predictive:+.3e}  "
            f"R2={self.r2_predictive:.4f}  t={self.t_predictive:+.1f}\n"
            f"  ratio predictive/contemporaneous R2 = "
            f"{(self.r2_predictive / self.r2 if self.r2 > 0 else 0):.3f}"
        )


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Through-origin OLS as in the paper. Returns (beta, r2, t)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < 3 or np.allclose(x, 0):
        return 0.0, 0.0, 0.0
    sxx = float(x @ x)
    beta = float(x @ y) / sxx
    resid = y - beta * x
    ss_res = float(resid @ resid)
    ss_tot = float(y @ y)               # through origin -> uncentred
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    dof = max(x.size - 1, 1)
    se = np.sqrt(ss_res / dof / sxx) if sxx > 0 else np.inf
    t = beta / se if se > 0 else 0.0
    return beta, r2, float(t)


def fit_ofi(buckets: Sequence[OFIBucket], depth_groups: int = 20) -> OFIFit:
    """Run the paper's regressions, PLUS the one the paper does not run.

    Regression 1:  dP_k     = beta * OFI_k        contemporaneous (the paper)
    Regression 2:  beta_i   = c / AD_i^lambda     across depth groups
    Regression 3:  dP_{k+1} = beta * OFI_k        PREDICTIVE (added here)

    Read this before quoting the 65% number at anyone, including yourself:

    Cont-Kukanov-Stoikov is a PRICE IMPACT result, not a forecasting result.
    OFI_k and dP_k are measured over the SAME interval. An R^2 of 65% says
    "when the queue emptied on one side, the price moved" -- which is close to
    a description of what a price move IS. It does not say you could have known
    beforehand.

    Regression 3 is the one that decides whether there is a trade. It is
    always far weaker, and on many instruments it is indistinguishable from
    zero. If `r2_predictive` is ~0 while `r2` is high, the correct conclusion
    is that OFI explains gold's moves and does not forecast them -- and the
    honest response is to stop, not to add features until something fits.

    Regression 2 is the part people skip and the part that tells you how to set
    thresholds: lambda near 1 confirms impact scales as 1/depth.
    """
    if len(buckets) < 30:
        raise ValueError(f"need >=30 buckets, got {len(buckets)}")

    ofi = np.array([b.ofi for b in buckets])
    dmid = np.array([b.d_mid for b in buckets])
    ad = np.array([b.avg_depth for b in buckets])
    norm = np.array([b.normalised for b in buckets])

    beta, r2, t = _ols(ofi, dmid)
    beta_n, r2_n, _ = _ols(norm, dmid)

    # Regression 3: does OFI_k say anything about the NEXT bucket's move?
    # Only pair buckets that are actually adjacent in time -- gaps (session
    # breaks, empty intervals) must not be treated as consecutive.
    starts = np.array([b.ts_start for b in buckets])
    ends = np.array([b.ts_end for b in buckets])
    adjacent = starts[1:] == ends[:-1]
    if adjacent.sum() >= 30:
        beta_p, r2_p, t_p = _ols(norm[:-1][adjacent], dmid[1:][adjacent])
    else:
        beta_p = r2_p = t_p = 0.0

    lam = c = r2_d = None
    ok = ad > 0
    if ok.sum() >= depth_groups * 10:
        # bucket the buckets by average depth, fit beta within each group
        qs = np.quantile(ad[ok], np.linspace(0, 1, depth_groups + 1))
        qs = np.unique(qs)
        betas, depths = [], []
        for lo, hi in zip(qs[:-1], qs[1:]):
            m = ok & (ad >= lo) & (ad < hi if hi < qs[-1] else ad <= hi)
            if m.sum() < 30:
                continue
            b_i, _, _ = _ols(ofi[m], dmid[m])
            if b_i > 0:
                betas.append(b_i)
                depths.append(float(np.mean(ad[m])))
        if len(betas) >= 4:
            # log beta = log c - lambda * log AD
            X = np.log(np.asarray(depths))
            Y = np.log(np.asarray(betas))
            A = np.vstack([np.ones_like(X), X]).T
            coef, *_ = np.linalg.lstsq(A, Y, rcond=None)
            c = float(np.exp(coef[0]))
            lam = float(-coef[1])
            pred = A @ coef
            ss_res = float(((Y - pred) ** 2).sum())
            ss_tot = float(((Y - Y.mean()) ** 2).sum())
            r2_d = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return OFIFit(beta=beta, r2=r2, n=len(buckets), t_stat=t,
                  beta_normalised=beta_n, r2_normalised=r2_n,
                  lambda_hat=lam, c_hat=c, r2_depth=r2_d,
                  beta_predictive=beta_p, r2_predictive=r2_p,
                  t_predictive=t_p)


class RollingOFIZScore:
    """Depth-normalised OFI as a live, causal z-score.

    This is the object the trading signal actually reads. It is causal by
    construction: the mean and variance at time t use only buckets strictly
    before t. Anything else is look-ahead, and look-ahead through a rolling
    normaliser is the single most common way a book-based backtest lies.
    """

    def __init__(self, window: int = 600, min_obs: int = 60):
        self.window = window
        self.min_obs = min_obs
        self._buf: list[float] = []

    def update(self, bucket: OFIBucket) -> float | None:
        x = bucket.normalised
        z: float | None = None
        if len(self._buf) >= self.min_obs:
            arr = np.asarray(self._buf)
            sd = float(arr.std(ddof=1))
            z = (x - float(arr.mean())) / sd if sd > 1e-12 else 0.0
        self._buf.append(x)
        if len(self._buf) > self.window:
            self._buf.pop(0)
        return z
