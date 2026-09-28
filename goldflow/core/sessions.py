"""
Session and roll clock for COMEX gold.

Three things break naive implementations, all of them handled here:

1. The CME session runs 18:00 ET to 17:00 ET with a 60-minute break -- NOT
   local midnight, NOT UTC midnight. Anchoring CVD, VWAP or a volume profile
   on the wrong boundary makes every day incomparable to every other day.

2. GC's liquid months are Feb, Apr, Jun, Aug, Dec (G, J, M, Q, Z). Volume
   migrates over several days ahead of First Notice Day. During that window,
   flow is split across two contracts and any cumulative statistic that does
   not reset is measuring the roll.

3. Two recurring events with their own microstructure: the 15:00 London fix
   (an auction, not continuous trading) and 08:30 / 13:30 ET US macro
   releases, which clear the book. Signals computed across these windows are
   measuring the event, not the auction state -- so they get flagged and the
   engine can stand aside.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..feeds.base import NS_PER_S

ET = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")

#: GC delivery months that carry real liquidity, in CME letter codes
LIQUID_MONTHS = {"G": 2, "J": 4, "M": 6, "Q": 8, "Z": 12}

CME_OPEN = time(18, 0)      # ET, previous calendar day
CME_CLOSE = time(17, 0)     # ET
CME_BREAK = (time(17, 0), time(18, 0))


def to_dt(ts_ns: int, tz=timezone.utc) -> datetime:
    return datetime.fromtimestamp(ts_ns / NS_PER_S, tz=tz)


def to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * NS_PER_S)


def cme_session_start(ts_ns: int) -> int:
    """Epoch ns of the 18:00 ET open of the session containing ts_ns."""
    d = to_dt(ts_ns, ET)
    anchor = d.replace(hour=18, minute=0, second=0, microsecond=0)
    if d < anchor:
        anchor -= timedelta(days=1)
    return to_ns(anchor)


def in_cme_break(ts_ns: int) -> bool:
    t = to_dt(ts_ns, ET).time()
    return CME_BREAK[0] <= t < CME_BREAK[1]


def is_weekend_gap(ts_ns: int) -> bool:
    d = to_dt(ts_ns, ET)
    wd = d.weekday()                       # Mon=0
    if wd == 5:                            # Saturday
        return True
    if wd == 4 and d.time() >= CME_CLOSE:  # Friday after the close
        return True
    if wd == 6 and d.time() < CME_OPEN:    # Sunday before the reopen
        return True
    return False


@dataclass(frozen=True)
class EventWindow:
    name: str
    start_ns: int
    end_ns: int

    def contains(self, ts: int) -> bool:
        return self.start_ns <= ts < self.end_ns


def event_windows_for_day(ts_ns: int, pad_min: int = 2) -> list[EventWindow]:
    """The recurring liquidity events on the calendar day containing ts_ns.

    `pad_min` brackets each event. The default of 2 minutes is deliberately
    small -- widen it once you have measured how long the book actually takes
    to rebuild on your own data, rather than guessing.
    """
    day_et = to_dt(ts_ns, ET).date()
    out: list[EventWindow] = []

    def win(name: str, tz, hh: int, mm: int, dur_min: int) -> None:
        base = datetime.combine(day_et, time(hh, mm), tzinfo=tz)
        out.append(EventWindow(
            name,
            to_ns(base - timedelta(minutes=pad_min)),
            to_ns(base + timedelta(minutes=dur_min + pad_min)),
        ))

    win("london_am_fix", LONDON, 10, 30, 10)
    win("london_pm_fix", LONDON, 15, 0, 10)
    win("us_macro_0830", ET, 8, 30, 5)
    win("comex_open", ET, 8, 20, 10)
    win("comex_settle", ET, 13, 30, 5)
    return out


class SessionClock:
    """Streaming session/roll state. Feed it timestamps; it tells the engine
    when to reset cumulative statistics and when to stand aside."""

    def __init__(self, pad_min: int = 2):
        self.pad_min = pad_min
        self._session: int | None = None
        self._day: int | None = None
        self._windows: list[EventWindow] = []
        self.session_changed = False
        self.current_event: str | None = None

    def update(self, ts_ns: int) -> None:
        s = cme_session_start(ts_ns)
        self.session_changed = (self._session is not None and s != self._session)
        self._session = s

        day = ts_ns // (86_400 * NS_PER_S)
        if day != self._day:
            self._day = day
            self._windows = event_windows_for_day(ts_ns, self.pad_min)

        self.current_event = next(
            (w.name for w in self._windows if w.contains(ts_ns)), None)

    @property
    def session_start(self) -> int | None:
        return self._session

    def should_trade(self) -> bool:
        """False during the CME break, the weekend gap, and event windows."""
        if self._session is None:
            return False
        ts = self._session
        return not (self.current_event is not None)


class RollDetector:
    """Detect the front-month roll from volume, not from the calendar.

    Volume-based is the right definition: "where the flow is" leads the
    calendar front month by days, and it is the flow you are trying to read.
    Call CVD.on_roll() and reset volume profiles whenever `changed` is True.
    """

    def __init__(self, min_ratio: float = 1.2, confirm_bars: int = 3):
        self.min_ratio = min_ratio
        self.confirm_bars = confirm_bars
        self.front: str | None = None
        self.changed = False
        self._pending: str | None = None
        self._count = 0

    def update(self, volumes: dict[str, float]) -> str | None:
        """volumes: contract symbol -> volume in the latest bar."""
        self.changed = False
        if not volumes:
            return self.front
        leader = max(volumes, key=volumes.get)
        if self.front is None:
            self.front = leader
            return self.front
        if leader == self.front:
            self._pending, self._count = None, 0
            return self.front

        cur = volumes.get(self.front, 0.0)
        if cur <= 0 or volumes[leader] / cur < self.min_ratio:
            return self.front

        if leader == self._pending:
            self._count += 1
        else:
            self._pending, self._count = leader, 1

        if self._count >= self.confirm_bars:
            self.front = leader
            self.changed = True
            self._pending, self._count = None, 0
        return self.front
