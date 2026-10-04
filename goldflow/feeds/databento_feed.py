"""
Databento adapter -- historical batch/streaming and live.

Written against the documented GLBX.MDP3 schemas. The `databento` package is
imported lazily so the rest of goldflow works without it installed and without
a key. Nothing here runs until you call it with credentials.

Why this vendor gets a first-class adapter: it is the only one in the shortlist
that publishes MBO (order-by-order) for CME GC, which is the single data
requirement that absorption and iceberg work cannot be faked around.

Schema choice
-------------
  mbp-1  -> Quote + Trade, aggressor side included. This is what OFI needs.
            Start here. Roughly 1-2 orders of magnitude smaller than mbo.
  mbo    -> full order-by-order. Needed ONLY for queue position, iceberg
            detection and true absorption. Large and expensive; do not pull a
            month of it before mbp-1 has proven the premise.
  ohlcv-1s -> for a first cheap look at whether an idea is even worth data.

Symbol choice
-------------
  "GC.c.0"  continuous front month via Databento's smart symbology; the roll
            is handled for you but a discontinuity still exists in the price
            series. Reset cumulative stats at roll regardless (see
            core.delta.CVD.on_roll).
  "GC.v.0"  volume-based front month -- usually the better definition of
            "where the flow is" than calendar front month.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .base import Quote, Side, Trade

_SIDE_MAP = {"A": Side.ASK, "B": Side.BID, "N": Side.UNKNOWN}

# CME prices arrive as fixed-point integers with 9 implied decimals.
_PX_SCALE = 1e-9
_UNDEF_PRICE = 9223372036854775807


@dataclass
class DatabentoConfig:
    dataset: str = "GLBX.MDP3"
    symbols: str = "GC.v.0"
    stype_in: str = "continuous"
    schema: str = "mbp-1"
    api_key: str | None = None      # falls back to DATABENTO_API_KEY env var


class DatabentoHistorical:
    """Replay historical Databento data as canonical events.

    Example
    -------
        feed = DatabentoHistorical(
            DatabentoConfig(schema="mbp-1", symbols="GC.v.0"),
            start="2026-06-01", end="2026-06-08",
        )
        for ev in feed.stream():
            ...

    Cost discipline: call `cost()` first. MBO for GC over a month is large and
    Databento bills on data volume; a surprise here is self-inflicted.
    """

    def __init__(self, cfg: DatabentoConfig, start: str, end: str):
        self.cfg = cfg
        self.start = start
        self.end = end
        self.symbol = cfg.symbols

    def _client(self):
        try:
            import databento as db
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "pip install databento -- adapter is optional and lazily loaded"
            ) from exc
        return db.Historical(self.cfg.api_key)

    def cost(self) -> float:
        """USD cost of this request, per the vendor, BEFORE downloading."""
        c = self.cfg
        return float(self._client().metadata.get_cost(
            dataset=c.dataset, symbols=c.symbols, schema=c.schema,
            start=self.start, end=self.end, stype_in=c.stype_in,
        ))

    def _records(self):
        c = self.cfg
        store = self._client().timeseries.get_range(
            dataset=c.dataset, symbols=c.symbols, schema=c.schema,
            start=self.start, end=self.end, stype_in=c.stype_in,
        )
        return store

    def stream(self) -> Iterator[Quote | Trade]:
        for rec in self._records():
            yield from _decode(rec, self.symbol)


class DatabentoLive:
    """Live MDP 3.0 stream. Same event contract as historical.

    The engine cannot tell the difference between this and a replay, which is
    the property that makes the backtest meaningful.
    """

    def __init__(self, cfg: DatabentoConfig):
        self.cfg = cfg
        self.symbol = cfg.symbols

    def stream(self) -> Iterator[Quote | Trade]:  # pragma: no cover - needs key
        try:
            import databento as db
        except ImportError as exc:
            raise ImportError("pip install databento") from exc

        c = self.cfg
        client = db.Live(key=c.api_key)
        client.subscribe(dataset=c.dataset, schema=c.schema,
                         stype_in=c.stype_in, symbols=c.symbols)
        for rec in client:
            yield from _decode(rec, self.symbol)


def _decode(rec, symbol: str) -> Iterator[Quote | Trade]:
    """Turn one Databento MBP-1 record into canonical events.

    Order matters: the trade is yielded BEFORE the resulting book state, which
    is the true causal sequence -- the print happens, then the queue reflects
    it. Getting this backwards makes absorption logic read inverted.
    """
    ts_event = int(getattr(rec, "ts_event", 0))
    ts_recv = int(getattr(rec, "ts_recv", ts_event) or ts_event)

    action = getattr(rec, "action", None)
    if action in ("T", b"T") or getattr(rec, "size", 0) and action == "T":
        px = getattr(rec, "price", None)
        if px is not None and px != _UNDEF_PRICE:
            yield Trade(
                ts_event, ts_recv, px * _PX_SCALE, int(rec.size),
                _SIDE_MAP.get(_as_str(getattr(rec, "side", "N")), Side.UNKNOWN),
                symbol,
            )

    levels = getattr(rec, "levels", None)
    if levels:
        top = levels[0]
        bp, ap = top.bid_px, top.ask_px
        if bp != _UNDEF_PRICE and ap != _UNDEF_PRICE:
            yield Quote(ts_event, ts_recv, bp * _PX_SCALE, ap * _PX_SCALE,
                        int(top.bid_sz), int(top.ask_sz), symbol)


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
