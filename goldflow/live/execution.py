"""
Execution abstraction and a paper broker.

Since the execution layer is not MQL5, the broker becomes a plug-in the same
way the data feed is. Implement `Broker` against whichever REST/FIX/WebSocket
API you end up with and nothing upstream changes.

What a gold execution API must give you, in priority order -- use this as the
checklist when evaluating one:

  1. A streaming quote for the exact symbol you will trade, with its OWN
     timestamps. Without this you cannot measure your basis or your slippage.
  2. Order acknowledgement latency you can measure. If the API will not tell
     you when the order reached the venue, you cannot model execution at all.
  3. Fill reports with the actual fill price, not the requested price.
  4. Position and balance queries that are authoritative, so the local view can
     be reconciled rather than trusted.
  5. Rejection reasons. "Order failed" with no reason makes live debugging
     guesswork.

`PaperBroker` implements all five against a live quote stream and is the right
place to run for the first weeks -- it exercises the whole path including the
staleness and reconciliation logic, without capital at risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..feeds.base import NS_PER_MS, Quote
from ..research.costs import CostConfig, SpreadModel


@dataclass
class Order:
    ts_ns: int
    lots: float                 # signed: + buy, - sell
    symbol: str
    kind: str = "market"
    limit_px: float | None = None
    tag: str = ""


@dataclass
class Execution:
    order: Order
    ts_ns: int
    fill_px: float
    lots: float
    ok: bool
    reason: str | None = None
    latency_ms: float = 0.0


@runtime_checkable
class Broker(Protocol):
    def submit(self, order: Order) -> Execution: ...
    def position(self, symbol: str) -> float: ...
    def flatten(self, symbol: str) -> Execution | None: ...


@dataclass
class PaperBroker:
    """Fills against the live quote with a modelled cost. No capital at risk.

    Deliberately pessimistic in two ways that matter:
      * fills at the far side of the spread plus modelled slippage, never at
        the mid;
      * rejects when the quote is stale, exactly as a real venue would reject
        or requote.
    """

    costs: CostConfig = field(default_factory=CostConfig)
    max_quote_age_ms: float = 2_000.0
    _spreads: SpreadModel = field(init=False)
    _pos: dict[str, float] = field(default_factory=dict)
    _last_quote: dict[str, Quote] = field(default_factory=dict)
    executions: list[Execution] = field(default_factory=list)
    realised_pnl: float = 0.0
    _avg_px: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._spreads = SpreadModel(self.costs)

    def on_quote(self, q: Quote) -> None:
        self._last_quote[q.symbol] = q

    def position(self, symbol: str) -> float:
        return self._pos.get(symbol, 0.0)

    def submit(self, order: Order) -> Execution:
        q = self._last_quote.get(order.symbol)
        if q is None:
            return self._reject(order, "no_quote")

        age_ms = (order.ts_ns - q.ts_recv) / NS_PER_MS
        if age_ms > self.max_quote_age_ms:
            return self._reject(order, f"stale_quote_{age_ms:.0f}ms")
        if q.is_crossed():
            return self._reject(order, "crossed_book")

        raw = q.ask_px if order.lots > 0 else q.bid_px
        slip = self._spreads.half_spread(order.ts_ns) * self.costs.slippage_mult
        fill = raw + (slip if order.lots > 0 else -slip)

        self._apply(order.symbol, order.lots, fill)

        ex = Execution(order=order, ts_ns=order.ts_ns, fill_px=fill,
                       lots=order.lots, ok=True, latency_ms=0.0)
        self.executions.append(ex)
        return ex

    def flatten(self, symbol: str) -> Execution | None:
        pos = self.position(symbol)
        if abs(pos) < 1e-9:
            return None
        q = self._last_quote.get(symbol)
        ts = q.ts_recv if q else 0
        return self.submit(Order(ts_ns=ts, lots=-pos, symbol=symbol,
                                 tag="flatten"))

    # -- internals -------------------------------------------------------
    def _apply(self, symbol: str, lots: float, px: float) -> None:
        pos = self._pos.get(symbol, 0.0)
        avg = self._avg_px.get(symbol, 0.0)
        new = pos + lots

        if pos == 0 or (pos > 0) == (lots > 0):
            # opening or adding
            self._avg_px[symbol] = ((abs(pos) * avg + abs(lots) * px)
                                    / (abs(pos) + abs(lots))) if new else px
        else:
            closed = min(abs(pos), abs(lots))
            self.realised_pnl += closed * (px - avg) * (1 if pos > 0 else -1)
            self.realised_pnl -= self.costs.commission_per_lot * closed
            if abs(new) > 1e-9 and (new > 0) != (pos > 0):
                self._avg_px[symbol] = px       # flipped
        self._pos[symbol] = new
        if abs(new) < 1e-9:
            self._avg_px.pop(symbol, None)

    def _reject(self, order: Order, reason: str) -> Execution:
        ex = Execution(order=order, ts_ns=order.ts_ns, fill_px=float("nan"),
                       lots=0.0, ok=False, reason=reason)
        self.executions.append(ex)
        return ex


class PositionReconciler:
    """Compare the local view against the broker's, and refuse to trade on a
    mismatch.

    Every bridged system eventually desynchronises -- a rejected order the
    local side counted, a partial fill, a restart. Trading on a wrong position
    is worse than not trading. On a mismatch, flatten and stop; do not attempt
    to reason about which side is right in the middle of a session.
    """

    def __init__(self, tolerance: float = 1e-6):
        self.tolerance = tolerance
        self.mismatches = 0

    def check(self, local: float, remote: float) -> tuple[bool, str | None]:
        if abs(local - remote) <= self.tolerance:
            return True, None
        self.mismatches += 1
        return False, (f"position mismatch: local={local:+.4f} "
                       f"broker={remote:+.4f} -- flatten and halt")
