"""
VPIN -- volume-synchronised probability of informed trading.

    VPIN = sum_{tau=1..n} |V_buy_tau - V_sell_tau| / (n * V)

Buckets are equal-VOLUME, not equal-time. That is the whole idea: it puts the
clock on trading activity rather than on the wall, so a quiet hour and a busy
minute get comparable weight.

Status, stated plainly: contested. Easley, Lopez de Prado & O'Hara claimed it
forecast the 2010 Flash Crash; Andersen & Bondarenko argued the result does not
survive careful specification and that VPIN largely tracks volatility.

Therefore this implementation is wired as a REGIME FILTER, not a directional
signal. `is_toxic()` answers one question -- is flow currently adversely
selecting the market makers, such that spreads widen and depth evaporates --
and the correct response to True is to stand aside, not to take a side. That
use is not what the critics dispute.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from ..feeds.base import Side, Trade


@dataclass(slots=True)
class VolumeBucket:
    ts_start: int
    ts_end: int
    buy: float
    sell: float

    @property
    def total(self) -> float:
        return self.buy + self.sell

    @property
    def imbalance(self) -> float:
        return abs(self.buy - self.sell)


class VPIN:
    """Streaming VPIN.

    Parameters
    ----------
    bucket_volume:
        V. Rule of thumb from the literature: one fiftieth of average daily
        volume, so that ~50 buckets span a day. For GC front month, measure
        it from your own data rather than copying a number for ES.
    window:
        n, number of buckets in the rolling average. 50 is the common choice.
    toxic_quantile:
        VPIN is only interpretable relative to its own history. A raw level
        means nothing; the CDF of the level does. 0.90 means "flow is more
        toxic than 90% of recent history".
    """

    def __init__(self, bucket_volume: float, window: int = 50,
                 toxic_quantile: float = 0.90, history: int = 1_000):
        if bucket_volume <= 0:
            raise ValueError("bucket_volume must be > 0")
        self.V = float(bucket_volume)
        self.window = int(window)
        self.toxic_quantile = float(toxic_quantile)
        self._buckets: deque[VolumeBucket] = deque(maxlen=window)
        self._hist: deque[float] = deque(maxlen=history)
        self._buy = 0.0
        self._sell = 0.0
        self._start_ts: int | None = None
        self.value: float | None = None

    def update(self, t: Trade) -> float | None:
        """Feed one classified trade. Returns VPIN when a bucket closes."""
        if self._start_ts is None:
            self._start_ts = t.ts_recv

        remaining = float(t.size)
        emitted: float | None = None

        # A single large print can complete more than one bucket. Splitting it
        # across buckets -- rather than assigning it whole -- is what keeps the
        # buckets equal-volume, which is the entire premise.
        while remaining > 0:
            capacity = self.V - (self._buy + self._sell)
            take = min(remaining, capacity)
            if t.side == Side.ASK:
                self._buy += take
            elif t.side == Side.BID:
                self._sell += take
            else:
                # unknown side splits evenly -- the only place this is
                # acceptable, because VPIN uses |buy - sell| and an unknown
                # print carries no directional information
                self._buy += take / 2
                self._sell += take / 2
            remaining -= take

            if (self._buy + self._sell) >= self.V - 1e-9:
                self._buckets.append(VolumeBucket(
                    self._start_ts, t.ts_recv, self._buy, self._sell))
                self._buy = self._sell = 0.0
                self._start_ts = t.ts_recv
                emitted = self._compute()

        return emitted

    def _compute(self) -> float | None:
        if len(self._buckets) < self.window:
            return None
        num = sum(b.imbalance for b in self._buckets)
        self.value = num / (len(self._buckets) * self.V)
        self._hist.append(self.value)
        return self.value

    def percentile(self) -> float | None:
        """Where the current level sits in its own recent history, in [0, 1]."""
        if self.value is None or len(self._hist) < 50:
            return None
        arr = np.asarray(self._hist)
        return float((arr <= self.value).mean())

    def is_toxic(self) -> bool:
        p = self.percentile()
        return p is not None and p >= self.toxic_quantile


def vpin_offline(prices, volumes, bucket_volume: float, window: int = 50,
                 df: float = 0.25) -> np.ndarray:
    """VPIN from bar data via bulk volume classification.

    For when you only have bars. Strictly worse than the streaming version on
    tick data, and included so the two can be compared on the same sample --
    which is the honest way to decide whether bar data is good enough for you.
    """
    from .classify import bulk_volume_classification

    bvc = bulk_volume_classification(prices, volumes, df=df)
    v = np.asarray(volumes, float)
    cum = np.cumsum(v)
    edges = np.arange(bucket_volume, cum[-1], bucket_volume)
    idx = np.searchsorted(cum, edges)

    imb, tot = [], []
    prev = 0
    for i in idx:
        if i <= prev:
            continue
        b = float(bvc.buy_volume[prev:i].sum())
        s = float(bvc.sell_volume[prev:i].sum())
        imb.append(abs(b - s))
        tot.append(b + s)
        prev = i

    imb = np.asarray(imb)
    if imb.size < window:
        return np.zeros(0)
    out = np.convolve(imb, np.ones(window), "valid") / (window * bucket_volume)
    return out
