"""
Generic CSV / Parquet adapter with a configurable column map.

This is the adapter you will actually use first, whichever API you pick:
almost every vendor can export or be dumped to a flat file, and getting the
research done on files is faster and cheaper than getting it done on a live
socket. Point `ColumnMap` at whatever the vendor calls its columns.

Handles the two things vendor files reliably get wrong:
  * timestamps in mixed units (s / ms / us / ns, or ISO strings)
  * unsorted rows around session boundaries
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from .base import NS_PER_S, Quote, Side, Trade


@dataclass
class ColumnMap:
    """Vendor column names -> canonical names.

    Only `ts_event` is mandatory. Provide the quote columns for an L1 file, the
    trade columns for a tape file, or all of them for a combined file.
    """

    ts_event: str = "ts_event"
    ts_recv: str | None = "ts_recv"
    bid_px: str | None = "bid_px_00"
    ask_px: str | None = "ask_px_00"
    bid_sz: str | None = "bid_sz_00"
    ask_sz: str | None = "ask_sz_00"
    price: str | None = "price"
    size: str | None = "size"
    side: str | None = "side"
    symbol: str | None = "symbol"

    #: how the vendor encodes aggressor side -> canonical Side
    side_values: dict = field(default_factory=lambda: {
        "A": Side.ASK, "B": Side.BID, "N": Side.UNKNOWN,
        "buy": Side.ASK, "sell": Side.BID,
        "1": Side.ASK, "-1": Side.BID, "0": Side.UNKNOWN,
        1: Side.ASK, -1: Side.BID, 0: Side.UNKNOWN,
    })


def _to_ns(series: pd.Series) -> np.ndarray:
    """Coerce anything a vendor calls a timestamp into int64 epoch ns."""
    if pd.api.types.is_numeric_dtype(series):
        v = series.to_numpy(dtype="float64")
        med = float(np.nanmedian(v))
        # pick the unit by order of magnitude of a plausible 2010-2040 epoch
        if med > 1e17:
            mult = 1                     # already ns
        elif med > 1e14:
            mult = 1_000                 # us
        elif med > 1e11:
            mult = 1_000_000             # ms
        else:
            mult = NS_PER_S              # s
        return (v * mult).astype("int64")
    return pd.to_datetime(series, utc=True, format="mixed").astype("int64").to_numpy()


class TabularFeed:
    """Replay a CSV or Parquet file as a canonical event stream."""

    def __init__(self, path: str | Path, colmap: ColumnMap | None = None,
                 symbol: str = "", kind: str = "auto", sort: bool = True):
        self.path = Path(path)
        self.colmap = colmap or ColumnMap()
        self.symbol = symbol
        self.kind = kind          # "quotes" | "trades" | "both" | "auto"
        self.sort = sort
        self._df: pd.DataFrame | None = None

    def load(self) -> pd.DataFrame:
        if self._df is not None:
            return self._df
        p = self.path
        if p.suffix in (".parquet", ".pq"):
            df = pd.read_parquet(p)
        else:
            df = pd.read_csv(p)

        m = self.colmap
        out = pd.DataFrame(index=df.index)
        out["ts_event"] = _to_ns(df[m.ts_event])
        out["ts_recv"] = (_to_ns(df[m.ts_recv])
                          if m.ts_recv and m.ts_recv in df.columns
                          else out["ts_event"])

        for canon, col in (("bid_px", m.bid_px), ("ask_px", m.ask_px),
                           ("bid_sz", m.bid_sz), ("ask_sz", m.ask_sz),
                           ("price", m.price), ("size", m.size)):
            if col and col in df.columns:
                out[canon] = pd.to_numeric(df[col], errors="coerce")

        if m.side and m.side in df.columns:
            out["side"] = df[m.side].map(m.side_values).fillna(Side.UNKNOWN)
        if m.symbol and m.symbol in df.columns:
            out["symbol"] = df[m.symbol].astype(str)
        else:
            out["symbol"] = self.symbol

        if self.sort:
            out = out.sort_values("ts_recv", kind="mergesort").reset_index(drop=True)
        self._df = out
        return out

    def _resolve_kind(self, df: pd.DataFrame) -> str:
        if self.kind != "auto":
            return self.kind
        has_q = {"bid_px", "ask_px"}.issubset(df.columns)
        has_t = {"price", "size"}.issubset(df.columns)
        if has_q and has_t:
            return "both"
        return "quotes" if has_q else "trades"

    def stream(self) -> Iterator[Quote | Trade]:
        df = self.load()
        kind = self._resolve_kind(df)
        cols = df.columns

        for row in df.itertuples(index=False):
            d = row._asdict()
            sym = d.get("symbol") or self.symbol
            if kind in ("trades", "both") and not pd.isna(d.get("price", np.nan)):
                yield Trade(
                    int(d["ts_event"]), int(d["ts_recv"]),
                    float(d["price"]), int(d.get("size") or 0),
                    Side(int(d.get("side", 0) or 0)), sym,
                )
            if kind in ("quotes", "both") and not pd.isna(d.get("bid_px", np.nan)):
                yield Quote(
                    int(d["ts_event"]), int(d["ts_recv"]),
                    float(d["bid_px"]), float(d["ask_px"]),
                    int(d.get("bid_sz") or 0), int(d.get("ask_sz") or 0), sym,
                )


# Ready-made column maps for the vendors worth considering. Verify against an
# actual sample file before trusting any of these -- vendors rename columns.
DATABENTO_MBP1 = ColumnMap(
    ts_event="ts_event", ts_recv="ts_recv",
    bid_px="bid_px_00", ask_px="ask_px_00",
    bid_sz="bid_sz_00", ask_sz="ask_sz_00",
    price="price", size="size", side="side", symbol="symbol",
)

POLYGON_QUOTES = ColumnMap(
    ts_event="sip_timestamp", ts_recv="participant_timestamp",
    bid_px="bid_price", ask_px="ask_price",
    bid_sz="bid_size", ask_sz="ask_size",
    price=None, size=None, side=None, symbol="ticker",
)

GENERIC_TICK = ColumnMap(
    ts_event="time", ts_recv=None,
    bid_px="bid", ask_px="ask", bid_sz=None, ask_sz=None,
    price=None, size=None, side=None, symbol=None,
)
