"""
Trade classification -- turning prints into signed order flow.

Ranking, per the published evaluations (Chakrabarty, Pascual & Shkilko 2015):

  exchange aggressor flag  >  Lee-Ready  >  tick rule  >  bulk volume (BVC)

If your feed carries the aggressor flag -- CME MDP 3.0 does -- USE IT and do
not run anything in this module. Every algorithm here exists to recover a
field you may already have, with error.

BVC is included because it is the honest fallback when you only have bars, and
because you will be tempted by "delta" indicators built on bar data. Seeing
what BVC actually is -- a Student-t CDF of the standardised bar return,
multiplied by bar volume -- makes clear that such a "delta" is a transformed
return series, not a measurement of who was aggressing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
from scipy import stats

from ..feeds.base import Quote, Side, Trade


class TickRule:
    """Sign by price change vs the previous trade; carry sign on no change."""

    def __init__(self) -> None:
        self._last_px: float | None = None
        self._last_side: Side = Side.UNKNOWN

    def classify(self, price: float) -> Side:
        if self._last_px is None:
            self._last_px = price
            return Side.UNKNOWN
        if price > self._last_px:
            self._last_side = Side.ASK
        elif price < self._last_px:
            self._last_side = Side.BID
        # equal -> keep previous side (the "zero tick" rule)
        self._last_px = price
        return self._last_side


class LeeReady:
    """Compare trade price to the prevailing midpoint; tick rule at the mid.

    The prevailing quote must be the one in effect BEFORE the trade. Using the
    quote that the trade itself caused inverts the classification -- a
    surprisingly common bug, and one that flips the sign of every delta series
    you build on top of it.
    """

    def __init__(self, quote_lag_ns: int = 0):
        self.quote_lag_ns = quote_lag_ns
        self._tick = TickRule()
        self._quote: Quote | None = None

    def on_quote(self, q: Quote) -> None:
        self._quote = q

    def classify(self, t: Trade) -> Side:
        q = self._quote
        if q is None or q.is_crossed():
            return self._tick.classify(t.price)
        if self.quote_lag_ns and (t.ts_recv - q.ts_recv) < self.quote_lag_ns:
            return self._tick.classify(t.price)
        mid = q.mid
        # keep the tick state warm regardless, so midpoint trades resolve
        tick_side = self._tick.classify(t.price)
        if t.price > mid:
            return Side.ASK
        if t.price < mid:
            return Side.BID
        return tick_side


@dataclass
class BVCResult:
    buy_volume: np.ndarray
    sell_volume: np.ndarray
    fraction_buy: np.ndarray


def bulk_volume_classification(prices: Sequence[float],
                               volumes: Sequence[float],
                               df: float = 0.25) -> BVCResult:
    """Easley, Lopez de Prado & O'Hara bulk volume classification.

        V_buy_tau  = V_tau * t_df( (P_tau - P_{tau-1}) / sigma_dP )
        V_sell_tau = V_tau - V_buy_tau

    `df` is the degrees of freedom of the Student-t. The original work uses a
    small value (heavy tails); 0.25 is the commonly cited choice and it matters
    -- df -> inf makes this a Gaussian CDF and materially changes the split.

    Only defensible when tick data genuinely does not exist. It does not beat
    Lee-Ready where it does.
    """
    p = np.asarray(prices, float)
    v = np.asarray(volumes, float)
    if p.size != v.size or p.size < 3:
        raise ValueError("prices and volumes must align and have >=3 points")

    dp = np.diff(p, prepend=p[0])
    sd = float(np.std(dp[1:], ddof=1))
    if sd <= 0:
        frac = np.full_like(p, 0.5)
    else:
        frac = stats.t.cdf(dp / sd, df=df)
    frac[0] = 0.5
    buy = v * frac
    return BVCResult(buy_volume=buy, sell_volume=v - buy, fraction_buy=frac)


def classify_stream(events: Iterable[Quote | Trade],
                    method: str = "auto",
                    quote_lag_ns: int = 0):
    """Yield (Trade with a resolved side) for every trade in a mixed stream.

    method="auto" keeps the exchange flag when present and falls back to
    Lee-Ready when it is not -- which is the correct policy, not a compromise.
    """
    lr = LeeReady(quote_lag_ns=quote_lag_ns)
    tick = TickRule()
    for ev in events:
        if isinstance(ev, Quote):
            lr.on_quote(ev)
            continue
        t: Trade = ev
        if method == "auto" and t.side != Side.UNKNOWN:
            yield t
            continue
        if method == "tick":
            side = tick.classify(t.price)
        else:
            side = lr.classify(t)
        yield Trade(t.ts_event, t.ts_recv, t.price, t.size, side, t.symbol)


def classification_agreement(events: Sequence[Quote | Trade]) -> dict:
    """Measure your fallback against the exchange flag on data that has both.

    Run this ONCE on a sample with aggressor flags before you rely on a
    fallback anywhere. If Lee-Ready agrees with the flag only 80% of the time
    on gold futures, every delta-derived signal carries that error, and you
    should know the number rather than assume the textbook one.
    """
    truth, lr_pred, tick_pred = [], [], []
    lr = LeeReady()
    tick = TickRule()
    for ev in events:
        if isinstance(ev, Quote):
            lr.on_quote(ev)
            continue
        if ev.side == Side.UNKNOWN:
            continue
        truth.append(int(ev.side))
        lr_pred.append(int(lr.classify(ev)))
        tick_pred.append(int(tick.classify(ev.price)))

    if not truth:
        return {"n": 0, "lee_ready": None, "tick_rule": None}
    t = np.asarray(truth)
    return {
        "n": int(t.size),
        "lee_ready": float((np.asarray(lr_pred) == t).mean()),
        "tick_rule": float((np.asarray(tick_pred) == t).mean()),
    }
