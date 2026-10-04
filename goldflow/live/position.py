"""
Bracket position management -- TP, SL, trailing, breakeven, time exit.

One class, used identically by the backtester and the live runner. That is
the point: a TP/SL scheme that exists only in the live path is untested, and
a backtest that ignores brackets is describing a different system from the
one you are running.

Exit precedence, and why it is ordered this way:

  1. STOP   -- checked first, always. When both the stop and the target are
               inside the same quote's range you cannot know which came first
               without tick-level intrabar data, so the stop wins. This makes
               the backtest pessimistic rather than optimistic. Resolving ties
               in favour of the target is the single most common way a bracket
               backtest inflates itself.
  2. TARGET
  3. TIME   -- the horizon the bracket was fitted at expires.
  4. SIGNAL -- the engine asks to flatten (risk gate, opposite signal).

Stops are evaluated against the EXIT side of the book -- bid for a long -- not
the mid. A stop measured on the mid is half a spread away from the stop you
will actually get, on every single trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..feeds.base import NS_PER_S, Quote


class ExitReason(str, Enum):
    STOP = "stop"
    TARGET = "target"
    TIME = "time"
    SIGNAL = "signal"
    TRAIL = "trail"
    NONE = ""


@dataclass
class BracketConfig:
    """All distances are in PRICE UNITS (USD/oz for gold), not pips or points.

    Fit `sl` and `tp` with research.excursion.recommend_bracket. Numbers picked
    by eye are the thing that module exists to replace.
    """

    sl: float = 1.50
    tp: float = 3.00
    #: hold limit; should match the horizon the bracket was fitted at
    max_hold_s: float = 60.0

    #: move the stop to entry once price has advanced this far. 0 disables.
    #: Not free -- it converts some winners into scratches, so it must be
    #: tested, not assumed to be prudent.
    breakeven_at: float = 0.0
    #: trail the stop this far behind the best price once breakeven is armed.
    #: 0 disables.
    trail_distance: float = 0.0

    #: widen the stop by this multiple of the current spread, so a temporary
    #: spread blowout does not take you out at a price nobody traded at
    spread_buffer_mult: float = 1.0


@dataclass
class ManagedPosition:
    side: int                 # +1 long, -1 short
    lots: float
    entry_px: float
    entry_ts: int
    cfg: BracketConfig
    stop_px: float = 0.0
    target_px: float = 0.0
    best_px: float = 0.0
    breakeven_armed: bool = False
    exit_reason: ExitReason = ExitReason.NONE
    exit_px: float = 0.0
    exit_ts: int = 0

    def __post_init__(self) -> None:
        if self.side > 0:
            self.stop_px = self.entry_px - self.cfg.sl
            self.target_px = self.entry_px + self.cfg.tp
        else:
            self.stop_px = self.entry_px + self.cfg.sl
            self.target_px = self.entry_px - self.cfg.tp
        self.best_px = self.entry_px

    # -- accessors -------------------------------------------------------
    def exit_price(self, q: Quote) -> float:
        """The price you would actually exit at: the far side of the book."""
        return q.bid_px if self.side > 0 else q.ask_px

    def unrealised(self, q: Quote) -> float:
        return (self.exit_price(q) - self.entry_px) * self.side * self.lots

    def advance(self, q: Quote) -> float:
        """How far the trade has gone in your favour, in price units."""
        return (self.exit_price(q) - self.entry_px) * self.side

    # -- the state machine ----------------------------------------------
    def update(self, q: Quote, flatten: bool = False) -> ExitReason:
        """Feed one spot quote. Returns the exit reason, or NONE to hold."""
        if self.exit_reason != ExitReason.NONE:
            return self.exit_reason

        px = self.exit_price(q)
        adv = self.advance(q)

        # track the best excursion for trailing
        if (px - self.best_px) * self.side > 0:
            self.best_px = px

        # arm breakeven
        if (self.cfg.breakeven_at > 0 and not self.breakeven_armed
                and adv >= self.cfg.breakeven_at):
            self.breakeven_armed = True
            self.stop_px = self.entry_px

        # trail
        if self.breakeven_armed and self.cfg.trail_distance > 0:
            trail = self.best_px - self.cfg.trail_distance * self.side
            if (trail - self.stop_px) * self.side > 0:
                self.stop_px = trail

        # A spread blowout should not trigger a stop at a price nobody traded.
        buf = q.spread * self.cfg.spread_buffer_mult
        eff_stop = self.stop_px - buf * self.side

        # 1. STOP first -- ties go to the stop, deliberately
        if (px - eff_stop) * self.side <= 0:
            return self._close(q, px, ExitReason.TRAIL if self.breakeven_armed
                               and self.cfg.trail_distance > 0 else ExitReason.STOP)

        # 2. TARGET
        if (px - self.target_px) * self.side >= 0:
            return self._close(q, px, ExitReason.TARGET)

        # 3. TIME
        if (q.ts_recv - self.entry_ts) >= self.cfg.max_hold_s * NS_PER_S:
            return self._close(q, px, ExitReason.TIME)

        # 4. SIGNAL
        if flatten:
            return self._close(q, px, ExitReason.SIGNAL)

        return ExitReason.NONE

    def _close(self, q: Quote, px: float, reason: ExitReason) -> ExitReason:
        self.exit_reason = reason
        self.exit_px = px
        self.exit_ts = q.ts_recv
        return reason

    @property
    def closed(self) -> bool:
        return self.exit_reason != ExitReason.NONE

    def realised(self) -> float:
        if not self.closed:
            return 0.0
        return (self.exit_px - self.entry_px) * self.side * self.lots


def size_for_risk(account_risk: float, sl_distance: float,
                  contract_size: float = 100.0,
                  max_lots: float = 1.0) -> float:
    """Lots such that hitting the stop loses `account_risk` currency units.

        lots = account_risk / (sl_distance * contract_size)

    For gold at a 100 oz lot: a 1.50 stop with 150 of risk gives 1.0 lot.

    This is fixed-fractional sizing, not Kelly. Kelly needs an edge estimate
    you do not have until the walk-forward is done, and Kelly on an
    overestimated edge is how accounts die faster than the edge can pay. Size
    by risk first; revisit sizing after the edge is measured out of sample.
    """
    if sl_distance <= 0 or contract_size <= 0:
        return 0.0
    return min(account_risk / (sl_distance * contract_size), max_lots)
