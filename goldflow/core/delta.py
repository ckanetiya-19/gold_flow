"""
Delta, cumulative volume delta, and the footprint matrix.

Delta = ask-executed volume minus bid-executed volume. CVD is its running sum.

The one thing that ruins CVD on futures and that no retail platform handles
for you: THE ROLL. When liquidity migrates from GCZ to GCG over several days,
an unstitched CVD is measuring contract migration, not buying pressure -- the
old contract bleeds sells and the new one absorbs buys purely as a mechanical
artefact. `CVD.on_roll()` exists for this and must be called.

The footprint matrix is included for completeness and for testing the patterns
the courses sell. Note what the signal catalogue says about them: no published
support. Build them, then test them, then most likely discard them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import numpy as np

from ..feeds.base import Side, Trade


@dataclass(slots=True)
class DeltaBar:
    ts_start: int
    ts_end: int
    buy_volume: int
    sell_volume: int
    trades: int
    open_px: float
    high_px: float
    low_px: float
    close_px: float

    @property
    def delta(self) -> int:
        return self.buy_volume - self.sell_volume

    @property
    def volume(self) -> int:
        return self.buy_volume + self.sell_volume

    @property
    def delta_pct(self) -> float:
        v = self.volume
        return self.delta / v if v else 0.0


class DeltaBarBuilder:
    """Time-bucketed delta bars from a classified trade stream."""

    def __init__(self, interval_ns: int):
        self.interval_ns = int(interval_ns)
        self._start: int | None = None
        self._buy = 0
        self._sell = 0
        self._n = 0
        self._o = self._h = self._l = self._c = float("nan")

    def _flush(self) -> DeltaBar | None:
        if self._n == 0 or self._start is None:
            return None
        return DeltaBar(self._start, self._start + self.interval_ns,
                        self._buy, self._sell, self._n,
                        self._o, self._h, self._l, self._c)

    def _reset(self, start: int, px: float) -> None:
        self._start = start
        self._buy = self._sell = self._n = 0
        self._o = self._h = self._l = self._c = px

    def update(self, t: Trade) -> DeltaBar | None:
        ts = t.ts_recv
        out = None
        if self._start is None:
            self._reset(ts - ts % self.interval_ns, t.price)
        elif ts >= self._start + self.interval_ns:
            out = self._flush()
            self._reset(ts - ts % self.interval_ns, t.price)

        if t.side == Side.ASK:
            self._buy += t.size
        elif t.side == Side.BID:
            self._sell += t.size
        # UNKNOWN contributes to neither -- see classify.py. Do not split it
        # 50/50; that manufactures a zero-delta bar out of real volume.
        self._n += 1
        self._h = max(self._h, t.price)
        self._l = min(self._l, t.price)
        self._c = t.price
        return out

    def run(self, trades: Iterable[Trade]) -> Iterator[DeltaBar]:
        for t in trades:
            b = self.update(t)
            if b is not None:
                yield b
        tail = self._flush()
        if tail is not None:
            yield tail


class CVD:
    """Cumulative volume delta with explicit session and roll handling.

    Anchoring policy is a choice, not a detail:
      * session  -- resets at the CME session boundary (18:00 ET). Honest
                    default; makes intraday CVD comparable across days.
      * roll     -- must be called manually when the front month changes.
                    Not resetting here is the single most common CVD bug.
    """

    def __init__(self) -> None:
        self.value = 0
        self.session_value = 0
        self._history: list[tuple[int, int]] = []
        self.rolls: list[int] = []
        self.session_starts: list[int] = []

    def update(self, bar: DeltaBar) -> int:
        self.value += bar.delta
        self.session_value += bar.delta
        self._history.append((bar.ts_end, self.value))
        return self.value

    def on_session_start(self, ts: int) -> None:
        self.session_value = 0
        self.session_starts.append(ts)

    def on_roll(self, ts: int, carry: bool = False) -> None:
        """Handle a front-month roll.

        carry=False (default) restarts the cumulative series -- the safe
        choice, because the two contracts' flows are not commensurable.
        carry=True keeps the level, which is only defensible if you have
        already verified the volume migration is complete.
        """
        self.rolls.append(ts)
        if not carry:
            self.value = 0
        self.session_value = 0

    def series(self) -> tuple[np.ndarray, np.ndarray]:
        if not self._history:
            return np.zeros(0, dtype="int64"), np.zeros(0, dtype="int64")
        ts, v = zip(*self._history)
        return np.asarray(ts, dtype="int64"), np.asarray(v, dtype="int64")


@dataclass
class Footprint:
    """bid x ask volume at each price level within one bar."""

    ts_start: int
    ts_end: int
    tick: float
    bid_vol: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    ask_vol: dict[float, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, t: Trade) -> None:
        px = round(round(t.price / self.tick) * self.tick, 6)
        if t.side == Side.ASK:
            self.ask_vol[px] += t.size
        elif t.side == Side.BID:
            self.bid_vol[px] += t.size

    def levels(self) -> list[float]:
        return sorted(set(self.bid_vol) | set(self.ask_vol))

    def poc(self) -> float | None:
        lv = self.levels()
        if not lv:
            return None
        return max(lv, key=lambda p: self.bid_vol[p] + self.ask_vol[p])

    def diagonal_imbalances(self, ratio: float = 3.0, min_vol: int = 10
                            ) -> list[tuple[float, str, float]]:
        """The classic 3:1 diagonal comparison.

        Ask volume at price p vs bid volume at p - 1 tick. Returned as
        (price, 'buy'|'sell', observed_ratio).

        The threshold is arbitrary and vendor-specific -- ATAS, Sierra and
        Quantower do not agree on the rule. Treat any result from this method
        as a hypothesis with a large multiple-testing problem attached, not as
        a signal. It is here so you can falsify it on your own data.
        """
        out: list[tuple[float, str, float]] = []
        lv = self.levels()
        for p in lv:
            below = round(p - self.tick, 6)
            a = self.ask_vol.get(p, 0)
            b = self.bid_vol.get(below, 0)
            if a >= min_vol and b > 0 and a / b >= ratio:
                out.append((p, "buy", a / b))
            a2 = self.ask_vol.get(below, 0)
            b2 = self.bid_vol.get(p, 0)
            if b2 >= min_vol and a2 > 0 and b2 / a2 >= ratio:
                out.append((p, "sell", b2 / a2))
        return out
