"""
Volume profile -- volume at price, POC, and the value area.

Steidlmayer's auction frame: a market advertises price to find volume. Price
moves quickly through levels where the auction is being rejected (low volume
nodes) and stalls where it is accepted (high volume nodes). POC is the price
with the most agreement; the value area is the central 70% of volume.

Evidence status: sound theory, thin formal testing. The defensible claim is
narrow and worth stating exactly -- these are better-motivated levels than
round numbers or a Fibonacci retracement, because they are derived from where
size actually traded. That is not the same as a demonstrated edge, and the
signal catalogue marks it accordingly.

The value area algorithm below is the standard one (expand from POC, always
taking the larger of the two adjacent pairs). Platforms differ in the details;
if you compare output against a chart package and it disagrees slightly, this
is why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from ..feeds.base import Trade


@dataclass
class VolumeProfile:
    tick: float
    value_area_pct: float = 0.70
    _vol: dict[float, int] = field(default_factory=dict)
    ts_start: int | None = None
    ts_end: int | None = None

    def add(self, t: Trade) -> None:
        px = round(round(t.price / self.tick) * self.tick, 6)
        self._vol[px] = self._vol.get(px, 0) + t.size
        if self.ts_start is None:
            self.ts_start = t.ts_recv
        self.ts_end = t.ts_recv

    def extend(self, trades: Iterable[Trade]) -> "VolumeProfile":
        for t in trades:
            self.add(t)
        return self

    # -- outputs ---------------------------------------------------------
    def as_arrays(self) -> tuple[np.ndarray, np.ndarray]:
        if not self._vol:
            return np.zeros(0), np.zeros(0, dtype=int)
        px = np.array(sorted(self._vol))
        vol = np.array([self._vol[p] for p in px], dtype=int)
        return px, vol

    @property
    def total_volume(self) -> int:
        return int(sum(self._vol.values()))

    def poc(self) -> float | None:
        """Point of control -- price with the most traded volume."""
        if not self._vol:
            return None
        return max(self._vol, key=self._vol.get)

    def value_area(self) -> tuple[float, float] | None:
        """(VAL, VAH) containing `value_area_pct` of volume, expanded from POC."""
        px, vol = self.as_arrays()
        if px.size == 0:
            return None
        target = self.value_area_pct * vol.sum()
        poc_i = int(np.argmax(vol))
        lo = hi = poc_i
        acc = float(vol[poc_i])

        while acc < target and (lo > 0 or hi < px.size - 1):
            # peek two levels each way, take the larger pair -- the standard rule
            down = vol[lo - 1] + (vol[lo - 2] if lo - 2 >= 0 else 0) if lo > 0 else -1
            up = vol[hi + 1] + (vol[hi + 2] if hi + 2 < px.size else 0) if hi < px.size - 1 else -1
            if up >= down and hi < px.size - 1:
                step = min(2, px.size - 1 - hi)
                acc += float(vol[hi + 1:hi + 1 + step].sum())
                hi += step
            elif lo > 0:
                step = min(2, lo)
                acc += float(vol[lo - step:lo].sum())
                lo -= step
            else:
                break
        return float(px[lo]), float(px[hi])

    def low_volume_nodes(self, quantile: float = 0.15) -> list[float]:
        """Prices the auction moved through quickly. Candidate breakout levels."""
        px, vol = self.as_arrays()
        if px.size < 10:
            return []
        thr = np.quantile(vol, quantile)
        return [float(p) for p, v in zip(px, vol) if v <= thr]

    def summary(self) -> dict:
        va = self.value_area()
        return {
            "poc": self.poc(),
            "val": va[0] if va else None,
            "vah": va[1] if va else None,
            "total_volume": self.total_volume,
            "levels": len(self._vol),
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
        }


def session_profiles(trades: Iterable[Trade], tick: float,
                     session_boundary_ns: int) -> list[VolumeProfile]:
    """Split a trade stream into per-session profiles.

    `session_boundary_ns` should come from sessions.cme_session_start -- the
    CME 18:00 ET open, not local midnight. Anchoring on the wrong boundary
    makes every POC and value area incomparable across days, which is the
    quiet way profile work stops meaning anything.
    """
    out: list[VolumeProfile] = []
    cur: VolumeProfile | None = None
    cur_bucket = None
    for t in trades:
        b = t.ts_recv // session_boundary_ns
        if cur is None or b != cur_bucket:
            cur = VolumeProfile(tick=tick)
            out.append(cur)
            cur_bucket = b
        cur.add(t)
    return out
