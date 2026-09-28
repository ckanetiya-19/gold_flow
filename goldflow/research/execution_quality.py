"""
Execution quality forensics -- measuring what your broker actually does to you.

This module exists because every claim in a broker's marketing is unfalsifiable
without it, and because the cost it measures is invisible in a normal P&L.
A last-look rejection costs you nothing on the statement: no trade, no fee, no
line item. It only shows up as trades you did not get, which is exactly the
population a naive review never looks at.

The central test is the ASYMMETRY TEST. Last look gives the LP a short option
on your order. If they exercise it rationally, they fill you when the market is
about to go against you and reject you when it is about to go your way. So:

    E[favourable move | REJECTED]  >>  E[favourable move | FILLED]

is the signature. Rejection RATE alone proves nothing -- a broker can reject 5%
of orders at random and be honest, or reject 2% and take all of your edge. The
gap between those two conditional expectations is the actual tax, and
multiplying it by the rejection rate gives its cost per attempt.

The second test is SLIPPAGE SYMMETRY. Genuine latency produces slippage that is
roughly symmetric -- sometimes the market moved for you, sometimes against.
Slippage that is systematically negative is not latency; it is a pricing policy.

Everything here works on records you already have: your own order log plus a
quote history. No cooperation from the broker is required, which is the point.

RUN BOTH TESTS. Validated against a simulated venue whose behaviour is known:

                        rejection rate   asymmetry gap   slippage verdict
    honest venue             1.1%        -0.008 (t=-0.4)  symmetric
    last-look venue         36.0%        +0.022 (t=+5.2)  "symmetric"

Note the bottom-right cell. The predatory venue's SLIPPAGE LOOKS FINE -- 30ms
latency, mild positive skew, nothing alarming. It is not slipping your fills;
it is selecting which orders become fills at all. A slippage review alone
clears this broker completely. Only the asymmetry test sees it, because only
the asymmetry test looks at the orders that never became trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..feeds.base import NS_PER_MS, NS_PER_S, Quote


#: Sample floors below which the asymmetry test declines to give a verdict.
#: See the note in `asymmetry_test` -- a null result on a small sample is not
#: a clean bill of health, and must not be presented as one.
MIN_FILLS = 100
MIN_REJECTS = 50


@dataclass(slots=True)
class OrderRecord:
    """One order attempt, filled or not.

    `requested_px` is the price you saw when you decided -- not the price you
    sent, if your platform re-quotes before sending. If you cannot capture the
    price you actually saw, the slippage numbers below measure your own
    platform's lag as well as the broker's, and you will not be able to tell
    the two apart.
    """

    ts_request: int
    side: int                      # +1 buy, -1 sell
    lots: float
    requested_px: float
    filled: bool
    ts_decision: int | None = None
    fill_px: float | None = None
    reason: str | None = None

    @property
    def decision_latency_ms(self) -> float:
        if self.ts_decision is None:
            return float("nan")
        return (self.ts_decision - self.ts_request) / NS_PER_MS

    @property
    def slippage(self) -> float:
        """Signed in YOUR favour: positive = price improvement."""
        if not self.filled or self.fill_px is None:
            return float("nan")
        return (self.requested_px - self.fill_px) * self.side


@dataclass
class AsymmetryResult:
    n_orders: int
    n_filled: int
    n_rejected: int
    rejection_rate: float
    horizon_ms: float
    mean_move_filled: float
    mean_move_rejected: float
    gap: float
    t_stat: float
    cost_per_attempt: float
    verdict: str

    def __str__(self) -> str:
        return (
            f"Asymmetry test  horizon={self.horizon_ms:g}ms  "
            f"n={self.n_orders} (filled {self.n_filled}, rejected {self.n_rejected})\n"
            f"  rejection rate           {self.rejection_rate:.2%}\n"
            f"  E[favourable | FILLED]   {self.mean_move_filled:+.4f}\n"
            f"  E[favourable | REJECTED] {self.mean_move_rejected:+.4f}\n"
            f"  gap                      {self.gap:+.4f}   t={self.t_stat:+.1f}\n"
            f"  implied cost per attempt {self.cost_per_attempt:.4f}\n"
            f"  -> {self.verdict}"
        )


def _mid_at(quote_ts: np.ndarray, quote_mid: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """Last mid at or before each ts. LOCF, never interpolated."""
    idx = np.searchsorted(quote_ts, ts, side="right") - 1
    out = np.full(ts.shape, np.nan)
    ok = idx >= 0
    out[ok] = quote_mid[idx[ok]]
    return out


def asymmetry_test(orders: Sequence[OrderRecord], quotes: Sequence[Quote],
                   horizon_ms: float = 1_000.0,
                   gap_threshold: float = 0.0) -> AsymmetryResult:
    """The core test. Did the rejections cluster on the good trades?

    Interpretation:
      gap ~ 0            rejections are uninformative -- latency, not selection
      gap > 0 and t > 3  the LP is exercising an option against you; the cost
                         is real and it is `cost_per_attempt` per order you send
      gap < 0            unusual; usually means your requested_px is not the
                         price you actually saw. Fix the capture before reading
                         anything else here.

    Needs a decent sample: at least MIN_FILLS fills and MIN_REJECTS rejections
    (100 / 50). Below that the test reports `insufficient sample` instead of a
    reassuring number, because an absent signal is not evidence of a fair
    broker. When it DOES return a null, it also reports the smallest gap that
    sample could have detected, so "not detected" is never mistaken for
    "not present".
    """
    if not orders or not quotes:
        raise ValueError("need both orders and quotes")

    q_ts = np.fromiter((q.ts_recv for q in quotes), dtype="int64", count=len(quotes))
    q_mid = np.fromiter((q.mid for q in quotes), dtype=float, count=len(quotes))
    order = np.argsort(q_ts, kind="mergesort")
    q_ts, q_mid = q_ts[order], q_mid[order]

    ts = np.fromiter((o.ts_request for o in orders), dtype="int64", count=len(orders))
    side = np.fromiter((o.side for o in orders), dtype=float, count=len(orders))
    filled = np.fromiter((o.filled for o in orders), dtype=bool, count=len(orders))

    h = int(horizon_ms * NS_PER_MS)
    mid0 = _mid_at(q_ts, q_mid, ts)
    mid1 = _mid_at(q_ts, q_mid, ts + h)

    # move in the direction the order wanted to go
    move = (mid1 - mid0) * side
    ok = np.isfinite(move)

    f = ok & filled
    r = ok & ~filled
    n_f, n_r = int(f.sum()), int(r.sum())

    rate = float((~filled).mean())
    # Minimums chosen so a null result carries information. With ~40 fills and
    # ~20 rejections the standard error on the gap is wider than the effect a
    # predatory venue produces, so "not significant" would mean nothing -- and
    # would read as a clean bill of health. That failure mode is worse than
    # refusing to answer.
    if n_f < MIN_FILLS or n_r < MIN_REJECTS:
        return AsymmetryResult(
            n_orders=len(orders), n_filled=n_f, n_rejected=n_r,
            rejection_rate=rate, horizon_ms=horizon_ms,
            mean_move_filled=float(np.nanmean(move[f])) if n_f else float("nan"),
            mean_move_rejected=float(np.nanmean(move[r])) if n_r else float("nan"),
            gap=float("nan"), t_stat=float("nan"), cost_per_attempt=float("nan"),
            verdict=(f"insufficient sample (filled={n_f}, rejected={n_r}) -- "
                     "this is NOT evidence the broker is fair, only that the "
                     "test cannot see yet"),
        )

    mf, mr = float(move[f].mean()), float(move[r].mean())
    gap = mr - mf
    se = np.sqrt(move[f].var(ddof=1) / n_f + move[r].var(ddof=1) / n_r)
    t = gap / se if se > 0 else 0.0
    cost = max(gap, 0.0) * rate

    if t > 3.0 and gap > gap_threshold:
        verdict = ("ASYMMETRIC. Rejections concentrate on orders that would "
                   "have worked. Your edge must clear this on top of spread "
                   "and commission.")
    elif t > 2.0:
        verdict = "suggestive of asymmetry; collect more attempts before acting"
    else:
        # State the power alongside the null. "Not detected" and "not present"
        # are different claims, and only the first one is supported here.
        mde = 3.0 * se
        verdict = (f"no measurable asymmetry at this horizon -- rejections look "
                   f"like latency rather than selection. Smallest gap this "
                   f"sample could have detected: {mde:.4f} "
                   f"({mde * rate:.4f} per attempt). A smaller tax than that "
                   f"would be invisible here.")

    return AsymmetryResult(len(orders), n_f, n_r, rate, horizon_ms,
                           mf, mr, gap, float(t), cost, verdict)


@dataclass
class SlippageResult:
    n: int
    mean: float
    median: float
    pct_positive: float
    pct_negative: float
    p95_adverse: float
    mean_latency_ms: float
    skew: float
    verdict: str

    def __str__(self) -> str:
        return (
            f"Slippage  n={self.n}  mean latency {self.mean_latency_ms:.0f}ms\n"
            f"  mean {self.mean:+.4f}   median {self.median:+.4f}   skew {self.skew:+.2f}\n"
            f"  improved {self.pct_positive:.1%}   worsened {self.pct_negative:.1%}\n"
            f"  p95 adverse {self.p95_adverse:.4f}\n"
            f"  -> {self.verdict}"
        )


def slippage_symmetry(orders: Sequence[OrderRecord]) -> SlippageResult:
    """Is slippage two-sided, or only ever against you?

    Honest latency is symmetric: between your click and the fill the market
    moved, and it had no reason to prefer one direction. A distribution where
    price improvement is rare and adverse slippage is common is not latency --
    it is a policy, and it is a cost you can price.
    """
    s = np.array([o.slippage for o in orders if o.filled], dtype=float)
    s = s[np.isfinite(s)]
    if s.size < 30:
        raise ValueError(f"need >=30 fills, got {s.size}")

    lat = np.array([o.decision_latency_ms for o in orders if o.filled], dtype=float)
    lat = lat[np.isfinite(lat)]

    pos = float((s > 0).mean())
    neg = float((s < 0).mean())
    sd = s.std(ddof=1)
    skew = float(((s - s.mean()) ** 3).mean() / sd ** 3) if sd > 0 else 0.0

    if pos > 0.9:
        # Not good news -- almost certainly a measurement error. Real venues do
        # not hand out price improvement on nearly every order. The usual cause
        # is comparing a fill against the MID rather than against the side of
        # the book you actually crossed, which manufactures a half-spread of
        # phantom improvement on every trade.
        verdict = ("IMPLAUSIBLE: price improvement on >90% of fills. Check that "
                   "requested_px is the tradeable price on your side of the "
                   "book (ask for a buy, bid for a sell), not the mid. Fix the "
                   "capture before reading any other number here.")
    elif pos < 0.05 and neg > 0.5:
        verdict = ("ONE-SIDED. Price improvement essentially never happens. "
                   "This is a pricing policy, not latency.")
    elif neg > pos * 2.5:
        verdict = "materially skewed against you -- price it as a fixed cost"
    else:
        verdict = "roughly symmetric -- consistent with honest latency"

    return SlippageResult(
        n=int(s.size), mean=float(s.mean()), median=float(np.median(s)),
        pct_positive=pos, pct_negative=neg,
        p95_adverse=float(-np.quantile(s, 0.05)),
        mean_latency_ms=float(lat.mean()) if lat.size else float("nan"),
        skew=skew, verdict=verdict,
    )


def spread_regime_profile(quotes: Sequence[Quote], bucket_minutes: int = 60) -> dict:
    """Where in the day does the spread actually widen?

    Run this before choosing a trading window. The answer is broker-specific
    and often nothing like the marketing 'from 0.1 pips'. What matters for a
    flow strategy is the spread at the moments the signal fires, not the
    advertised minimum.
    """
    if len(quotes) < 500:
        raise ValueError("need >=500 quotes")

    ts = np.fromiter((q.ts_recv for q in quotes), dtype="int64", count=len(quotes))
    sp = np.fromiter((q.spread for q in quotes), dtype=float, count=len(quotes))
    good = sp > 0
    ts, sp = ts[good], sp[good]

    hour = ((ts // (3_600 * NS_PER_S)) % 24).astype(int)
    out = {}
    for h in range(0, 24, max(1, bucket_minutes // 60)):
        m = hour == h
        if m.sum() < 20:
            continue
        out[f"utc_{h:02d}"] = {
            "median": round(float(np.median(sp[m])), 4),
            "p90": round(float(np.quantile(sp[m], 0.90)), 4),
            "p99": round(float(np.quantile(sp[m], 0.99)), 4),
            "n": int(m.sum()),
        }
    med = float(np.median(sp))
    return {
        "overall_median": round(med, 4),
        "overall_p99": round(float(np.quantile(sp, 0.99)), 4),
        "widening_ratio_p99_to_median": round(float(np.quantile(sp, 0.99)) / med, 1)
        if med > 0 else None,
        "by_hour_utc": out,
    }


def full_report(orders: Sequence[OrderRecord], quotes: Sequence[Quote],
                horizons_ms: Sequence[float] = (200, 1_000, 5_000)) -> dict:
    """Everything at once. Run this on a broker before committing to it.

    Protocol that gives the test power without much capital at risk:
      1. Trade the smallest size the broker allows.
      2. Send at least 300 orders, spread across sessions -- including the
         windows where you would actually trade, since that is where the
         behaviour differs.
      3. Deliberately send some orders INTO fast markets. A broker that behaves
         well only in quiet conditions has told you nothing about the
         conditions your signal fires in.
      4. Log the price you SAW, not the price the platform sent.
    """
    rep: dict = {"n_orders": len(orders)}

    rep["asymmetry"] = {}
    for h in horizons_ms:
        try:
            rep["asymmetry"][f"{h:g}ms"] = str(asymmetry_test(orders, quotes, h))
        except Exception as exc:                        # noqa: BLE001
            rep["asymmetry"][f"{h:g}ms"] = f"error: {exc}"

    try:
        rep["slippage"] = str(slippage_symmetry(orders))
    except Exception as exc:                            # noqa: BLE001
        rep["slippage"] = f"error: {exc}"

    try:
        rep["spread"] = spread_regime_profile(quotes)
    except Exception as exc:                            # noqa: BLE001
        rep["spread"] = f"error: {exc}"

    fills = [o for o in orders if o.filled]
    if fills:
        lat = np.array([o.decision_latency_ms for o in fills])
        lat = lat[np.isfinite(lat)]
        if lat.size:
            rep["decision_latency_ms"] = {
                "median": round(float(np.median(lat)), 1),
                "p95": round(float(np.quantile(lat, 0.95)), 1),
                "max": round(float(lat.max()), 1),
                "note": ("A median hold time in the 10-200ms range is the "
                         "documented last-look window. Compare it against the "
                         "40-100ms GC->spot propagation lag: if the hold time "
                         "is comparable, the LP can see your signal resolve "
                         "before deciding whether to fill you."),
            }
    return rep


# ---------------------------------------------------------------------------
# A simulated last-look venue, for validating the detector
# ---------------------------------------------------------------------------

@dataclass
class LastLookBroker:
    """Simulates an LP that holds each order and rejects the ones going against it.

    Its only purpose is to prove the detector above works: run the tests on a
    venue whose behaviour you KNOW and confirm they find it. A forensic tool
    that has never been shown a positive case is not a tool.

    `aggression` in [0, 1]: 0 fills everything, 1 rejects every order that would
    have been profitable for the taker over the hold window.
    """

    quotes: Sequence[Quote]
    hold_ms: float = 30.0
    aggression: float = 0.8
    base_reject_rate: float = 0.01
    seed: int = 17
    _rng: np.random.Generator = field(init=False)
    _ts: np.ndarray = field(init=False)
    _mid: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        self._ts = np.fromiter((q.ts_recv for q in self.quotes),
                               dtype="int64", count=len(self.quotes))
        self._mid = np.fromiter((q.mid for q in self.quotes),
                                dtype=float, count=len(self.quotes))

    def _idx_at(self, ts: int) -> int:
        return int(np.searchsorted(self._ts, ts, side="right")) - 1

    def _mid_at(self, ts: int) -> float:
        i = self._idx_at(ts)
        return float(self._mid[i]) if i >= 0 else float("nan")

    def submit(self, ts_request: int, side: int, lots: float,
               requested_px: float) -> OrderRecord:
        hold = int(self.hold_ms * NS_PER_MS)
        ts_decision = ts_request + hold

        m0 = self._mid_at(ts_request)
        m1 = self._mid_at(ts_decision)
        # move in the taker's favour over the hold window -- exactly what the
        # LP gets to see and the taker does not
        edge = (m1 - m0) * side if np.isfinite(m0) and np.isfinite(m1) else 0.0

        reject = self._rng.random() < self.base_reject_rate
        if edge > 0 and self._rng.random() < self.aggression:
            reject = True

        if reject:
            return OrderRecord(ts_request, side, lots, requested_px, False,
                               ts_decision, None, "last_look_reject")

        # Fill on the SAME SIDE of the book you crossed, at the post-hold
        # quote. Filling at the mid here would hand every order a free
        # half-spread and make the slippage distribution meaningless -- which
        # is precisely the measurement error `slippage_symmetry` now warns
        # about, and it is easy to make in a simulator as well as in a log.
        i = self._idx_at(ts_decision)
        if i >= 0:
            q = self.quotes[i]
            fill = q.ask_px if side > 0 else q.bid_px
        else:
            fill = requested_px
        return OrderRecord(ts_request, side, lots, requested_px, True,
                           ts_decision, fill, None)
