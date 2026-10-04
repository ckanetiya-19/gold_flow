"""
Execution cost model.

This is the module that kills most signals, and it should be applied BEFORE
you get attached to an equity curve, not after.

Gold's specific problem: the spread widens exactly when the flow signal is
strongest. A model with a constant spread will show an edge on macro releases
and fix windows that does not exist in your account. The default profile here
therefore multiplies the spread during event windows and during the thin
overnight hours, and it is deliberately pessimistic. Calibrate it from your
own broker's tick history via `SpreadModel.from_ticks` before trusting a
backtest number -- a model built from vendor data is a model of somebody
else's execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..core.sessions import SessionClock, to_dt, ET
from ..feeds.base import Quote


@dataclass
class CostConfig:
    #: half-spread in price units at the tightest, i.e. best case
    base_half_spread: float = 0.11
    #: commission per lot per side, in price units per ounce-equivalent
    commission_per_lot: float = 0.035
    #: slippage as a multiple of the half-spread on a market order
    slippage_mult: float = 0.5
    #: spread multiplier inside a scheduled event window
    event_spread_mult: float = 3.0
    #: spread multiplier during thin hours (ET 17:00-02:00)
    thin_hours_mult: float = 1.8
    #: Kyle impact per contract, from core.kyle. 0 = ignore own impact
    kyle_lambda: float = 0.0
    #: hard cap; a fill worse than this is treated as a rejected order
    max_half_spread: float = 2.50


@dataclass
class SpreadModel:
    cfg: CostConfig = field(default_factory=CostConfig)
    _clock: SessionClock = field(default_factory=SessionClock)

    @classmethod
    def from_ticks(cls, quotes: Sequence[Quote],
                   cfg: CostConfig | None = None) -> "SpreadModel":
        """Calibrate the base half-spread from your broker's own tick history.

        Uses the 25th percentile rather than the mean: the mean is dragged up
        by event spikes that the multipliers already model, so using it would
        double-count them.
        """
        cfg = cfg or CostConfig()
        s = np.array([q.spread for q in quotes if q.spread > 0])
        if s.size < 100:
            raise ValueError("need >=100 quotes to calibrate")
        cfg.base_half_spread = float(np.quantile(s, 0.25) / 2.0)
        cfg.max_half_spread = float(np.quantile(s, 0.999) / 2.0)
        return cls(cfg=cfg)

    def half_spread(self, ts_ns: int) -> float:
        c = self.cfg
        hs = c.base_half_spread
        self._clock.update(ts_ns)
        if self._clock.current_event is not None:
            hs *= c.event_spread_mult
        hour = to_dt(ts_ns, ET).hour
        if hour >= 17 or hour < 2:
            hs *= c.thin_hours_mult
        return min(hs, c.max_half_spread)

    def cost_per_side(self, ts_ns: int, lots: float = 1.0,
                      contracts: int = 0) -> float:
        """All-in cost of crossing, in price units, for one side of a round trip."""
        c = self.cfg
        hs = self.half_spread(ts_ns)
        cost = hs * (1.0 + c.slippage_mult) + c.commission_per_lot
        if c.kyle_lambda and contracts:
            cost += c.kyle_lambda * abs(contracts)
        return cost * abs(lots)

    def round_trip(self, ts_in: int, ts_out: int, lots: float = 1.0,
                   contracts: int = 0) -> float:
        return (self.cost_per_side(ts_in, lots, contracts)
                + self.cost_per_side(ts_out, lots, contracts))

    def breakeven_move(self, ts_ns: int, lots: float = 1.0) -> float:
        """Minimum favourable move, in price units, just to break even.

        Print this next to any signal's expected move. If the expected move is
        smaller, stop -- no amount of parameter tuning fixes a negative gross
        edge, and every further backtest you run is overfitting to noise.
        """
        return 2.0 * self.cost_per_side(ts_ns, lots)
