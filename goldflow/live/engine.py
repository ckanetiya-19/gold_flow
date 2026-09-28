"""
Live signal engine.

Consumes a futures feed and a spot feed, maintains exactly the same state
objects the backtester uses, and emits a signal packet on every closed bucket.

The critical property: `Backtester.run` and `SignalEngine.step` drive the SAME
OFIBucketer, the SAME RollingOFIZScore and the SAME SessionClock. If research
and production computed the signal with two different code paths -- as they do
in nearly every retail setup -- the backtest would be describing a system that
does not exist. Keeping one implementation is not tidiness; it is the only
reason the backtest means anything.

Staleness is a first-class risk here. A frozen bridge that keeps repeating its
last instruction is how accounts die. `SignalPacket.age_ms` and the halt logic
in risk.py exist for that and must never be bypassed.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterator

import numpy as np

from ..core.ofi import OFIBucketer, RollingOFIZScore
from ..core.sessions import SessionClock
from ..core.vpin import VPIN
from ..feeds.base import NS_PER_MS, NS_PER_S, Quote, Trade
from .risk import RiskConfig, RiskGate


@dataclass
class EngineConfig:
    bucket_ns: int = NS_PER_S
    zscore_window: int = 600
    zscore_min_obs: int = 60
    vpin_bucket_volume: float = 0.0        # 0 disables VPIN
    vpin_window: int = 50
    basis_window: int = 3_600
    risk: RiskConfig = field(default_factory=RiskConfig)


@dataclass
class SignalPacket:
    """What leaves the engine. Deliberately flat and JSON-serialisable so any
    consumer -- a Python broker client, a websocket, a log file -- reads the
    same thing without a shared object model."""

    ts_ns: int
    seq: int
    ofi_z: float | None
    ofi_normalised: float
    avg_depth: float
    fut_mid: float
    spot_mid: float | None
    basis: float | None
    basis_z: float | None
    vpin: float | None
    vpin_toxic: bool
    event_window: str | None
    halt: bool
    halt_reason: str | None
    #: engine-suggested target, in lots. A consumer is free to ignore it, but
    #: must never trade when halt is True.
    target: float

    def age_ms(self, now_ns: int | None = None) -> float:
        now = now_ns if now_ns is not None else time.time_ns()
        return (now - self.ts_ns) / NS_PER_MS

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class SignalEngine:
    def __init__(self, cfg: EngineConfig | None = None,
                 strategy: Callable | None = None):
        self.cfg = cfg or EngineConfig()
        self.strategy = strategy
        self.bucketer = OFIBucketer(self.cfg.bucket_ns)
        self.zscore = RollingOFIZScore(self.cfg.zscore_window,
                                       self.cfg.zscore_min_obs)
        self.clock = SessionClock()
        self.gate = RiskGate(self.cfg.risk)
        self.vpin = (VPIN(self.cfg.vpin_bucket_volume, self.cfg.vpin_window)
                     if self.cfg.vpin_bucket_volume > 0 else None)

        self._basis_hist: list[float] = []
        self._spot: Quote | None = None
        self._seq = 0
        self.position = 0.0

    # -- inputs ----------------------------------------------------------
    def on_spot_quote(self, q: Quote) -> None:
        self._spot = q

    def on_trade(self, t: Trade) -> None:
        if self.vpin is not None:
            self.vpin.update(t)

    def on_fut_quote(self, q: Quote) -> SignalPacket | None:
        """Feed a futures quote. Returns a packet when a bucket closes."""
        bucket = self.bucketer.update(q)
        if bucket is None:
            return None

        z = self.zscore.update(bucket)
        self.clock.update(bucket.ts_end)

        spot_mid = basis = basis_z = None
        if self._spot is not None:
            spot_mid = self._spot.mid
            basis = bucket.mid_end - spot_mid
            self._basis_hist.append(basis)
            if len(self._basis_hist) > self.cfg.basis_window:
                self._basis_hist.pop(0)
            if len(self._basis_hist) >= 200:
                arr = np.asarray(self._basis_hist[:-1])
                sd = float(arr.std(ddof=1))
                basis_z = (basis - float(arr.mean())) / sd if sd > 1e-9 else 0.0

        vpin_val = self.vpin.value if self.vpin else None
        vpin_toxic = bool(self.vpin.is_toxic()) if self.vpin else False

        halt, reason = self.gate.check(
            ts_ns=bucket.ts_end,
            spot=self._spot,
            basis_z=basis_z,
            event_window=self.clock.current_event,
            vpin_toxic=vpin_toxic,
        )

        target = 0.0
        if not halt and self.strategy is not None:
            from ..research.backtest import SignalState
            st = SignalState(
                ts_ns=bucket.ts_end,
                ofi_z=z,
                ofi_normalised=bucket.normalised,
                avg_depth=bucket.avg_depth,
                fut_mid=bucket.mid_end,
                spot_bid=self._spot.bid_px if self._spot else float("nan"),
                spot_ask=self._spot.ask_px if self._spot else float("nan"),
                basis=basis or 0.0,
                basis_z=basis_z,
                event_window=self.clock.current_event,
                vpin=vpin_val,
                vpin_toxic=vpin_toxic,
            )
            target = float(self.strategy(st, self.position))

        self._seq += 1
        return SignalPacket(
            ts_ns=bucket.ts_end,
            seq=self._seq,
            ofi_z=z,
            ofi_normalised=bucket.normalised,
            avg_depth=bucket.avg_depth,
            fut_mid=bucket.mid_end,
            spot_mid=spot_mid,
            basis=basis,
            basis_z=basis_z,
            vpin=vpin_val,
            vpin_toxic=vpin_toxic,
            event_window=self.clock.current_event,
            halt=halt,
            halt_reason=reason,
            target=target,
        )

    # -- driver ----------------------------------------------------------
    def run(self, fut_feed, spot_feed=None) -> Iterator[SignalPacket]:
        """Drive from one or two feeds merged on arrival time.

        Merging on ts_recv, not ts_event, for the same reason the backtester
        does: you can only act on what has arrived.
        """
        from ..feeds.base import merge_streams

        stream = merge_streams(fut_feed, spot_feed) if spot_feed else fut_feed.stream()
        fut_symbol = getattr(fut_feed, "symbol", None)

        for ev in stream:
            if isinstance(ev, Trade):
                self.on_trade(ev)
                continue
            if not isinstance(ev, Quote):
                continue
            if spot_feed is not None and ev.symbol != fut_symbol:
                self.on_spot_quote(ev)
                continue
            pkt = self.on_fut_quote(ev)
            if pkt is not None:
                yield pkt
