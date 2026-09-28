"""
Feed abstraction layer.

The whole point of this module: NOTHING downstream knows which vendor the data
came from. Core math and research code consume the three event types defined
here. When you pick a data API, you write ONE adapter that yields these events
and everything else keeps working unchanged.

Design rules enforced here
--------------------------
1. Timestamps are integer nanoseconds since UNIX epoch, UTC. Never float
   seconds -- float64 loses nanosecond resolution above ~2^53 ns (year 2255 is
   fine, but arithmetic on differences silently degrades). Lead-lag work is
   worthless without exact timestamps.
2. Every event carries BOTH an exchange timestamp (ts_event) and a local
   receipt timestamp (ts_recv). Any signal that uses ts_event for causality in
   a live system is cheating; any backtest that uses ts_recv is optimistic.
   We keep both so the harness can be explicit about which it used.
3. Prices are floats but sizes are ints (contracts / lots), because trade
   classification and queue arithmetic must be exact.
4. Events are immutable. Slots are used because a month of GC MBP-1 is tens of
   millions of objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Iterator, Protocol, runtime_checkable

NS_PER_US = 1_000
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


class Side(IntEnum):
    """Aggressor side of a trade.

    UNKNOWN is not a placeholder to be ignored -- it is the honest state for
    any feed that does not publish an aggressor flag, and the classification
    module exists specifically to turn it into BID/ASK with a stated error
    rate. Never silently coerce UNKNOWN to a side.
    """

    UNKNOWN = 0
    BID = -1   # aggressor sold into the resting bid  -> sell-initiated
    ASK = 1    # aggressor lifted the resting offer    -> buy-initiated


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book (L1) state after an event. MBP-1 in Databento terms."""

    ts_event: int
    ts_recv: int
    bid_px: float
    ask_px: float
    bid_sz: int
    ask_sz: int
    symbol: str = ""

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid_px + self.ask_px)

    @property
    def spread(self) -> float:
        return self.ask_px - self.bid_px

    @property
    def imbalance(self) -> float:
        """I = Qb / (Qb + Qa), in [0, 1]. 0.5 == balanced."""
        tot = self.bid_sz + self.ask_sz
        return 0.5 if tot <= 0 else self.bid_sz / tot

    def is_crossed(self) -> bool:
        return self.bid_px >= self.ask_px


@dataclass(frozen=True, slots=True)
class Trade:
    """A single executed print."""

    ts_event: int
    ts_recv: int
    price: float
    size: int
    side: Side = Side.UNKNOWN
    symbol: str = ""

    @property
    def signed_size(self) -> int:
        """+size for buy-initiated, -size for sell-initiated, 0 if unknown."""
        return int(self.side) * self.size


@dataclass(frozen=True, slots=True)
class BookLevel:
    px: float
    sz: int
    ct: int = 0     # order count at the level, when the feed provides it


@dataclass(frozen=True, slots=True)
class Depth:
    """Multi-level book snapshot (MBP-10 or deeper).

    Only needed for depth-normalisation beyond L1 and for absorption work.
    OFI itself needs only L1.
    """

    ts_event: int
    ts_recv: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    symbol: str = ""

    def total_depth(self, levels: int = 5) -> float:
        b = sum(l.sz for l in self.bids[:levels])
        a = sum(l.sz for l in self.asks[:levels])
        return float(b + a)


Event = Quote | Trade | Depth


@runtime_checkable
class Feed(Protocol):
    """The one interface an adapter must satisfy.

    Implement `stream()` as a generator that yields events in non-decreasing
    ts_recv order. That ordering guarantee is what makes the backtest causal;
    an adapter that violates it will produce look-ahead and the harness cannot
    detect every case for you.
    """

    symbol: str

    def stream(self) -> Iterator[Event]:
        ...


def merge_streams(*feeds: Feed) -> Iterator[Event]:
    """Merge several feeds into one ts_recv-ordered stream.

    Used to run a GC feed and a spot feed through the same causal loop. This is
    a k-way merge on ts_recv, NOT ts_event: in live trading you can only act on
    what has arrived. Backtests that merge on ts_event are measuring a machine
    you do not own.
    """
    import heapq

    def keyed(f: Feed, idx: int):
        for ev in f.stream():
            yield (ev.ts_recv, idx, ev)

    for _, _, ev in heapq.merge(*(keyed(f, i) for i, f in enumerate(feeds))):
        yield ev


def assert_monotonic(events: Iterable[Event], field: str = "ts_recv") -> int:
    """Validate an adapter. Returns the number of events checked.

    Run this once against every new adapter before trusting it. A vendor that
    ships out-of-order rows -- and several do around session boundaries and
    snapshot refreshes -- will silently corrupt every cumulative statistic.
    """
    last = -1
    n = 0
    for ev in events:
        ts = getattr(ev, field)
        if ts < last:
            raise ValueError(
                f"{field} went backwards at event {n}: {ts} < {last}. "
                "Adapter must sort or the harness is not causal."
            )
        last = ts
        n += 1
    return n
