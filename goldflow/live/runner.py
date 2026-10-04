"""
The automatic loop -- signal in, bracketed order out, position managed to exit.

This is the whole system as one object. It deliberately contains no strategy
logic and no order-flow maths: it wires together components that were each
tested on their own, and its only job is to do that wiring in the correct
order, with the correct failure behaviour.

ORDER OF OPERATIONS, and why

  1. Manage the OPEN position first, on every spot quote.
     Not on signal ticks -- on every quote. A stop checked once per second is
     not a stop. This must happen before anything else, because an exit that
     is due has already happened in the market whether or not you noticed.

  2. Then evaluate risk gates.
  3. Then, only if flat and un-gated, consider a new entry.

  Doing this in any other order lets a new signal open a position in the same
  pass that an exit was missed.

FAILURE BEHAVIOUR -- the part that matters more than the entries

  Every failure degrades to FLAT, never to "hold and hope":

    stale data        -> flatten and stop trading
    feed disconnect   -> flatten and stop trading
    position mismatch -> flatten and stop trading, do not reconcile mid-session
    basis blowout     -> flatten and stop trading
    daily loss hit    -> flatten and stop trading, no re-arm until reset

  A frozen bridge that keeps repeating its last instruction is how accounts
  die. `Heartbeat` is checked from the loop AND should be checked by whatever
  supervises the process, because a loop that has stopped receiving events has
  also stopped checking its own liveness.

WHAT THIS DOES NOT DO

  It does not size by Kelly, average down, pyramid, or re-enter a direction
  that just stopped it out. Each of those is a separate strategy decision that
  needs its own study; none of them belong in the plumbing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np

from ..feeds.base import NS_PER_MS, NS_PER_S, Quote, Trade
from .engine import EngineConfig, SignalEngine, SignalPacket
from .execution import Broker, Execution, Order, PositionReconciler
from .position import BracketConfig, ExitReason, ManagedPosition, size_for_risk
from .risk import Heartbeat, RiskConfig


@dataclass
class RunnerConfig:
    symbol: str = "XAUUSD"
    entry_z: float = 2.0
    bracket: BracketConfig = field(default_factory=BracketConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)

    #: currency risked per trade; 0 uses fixed `lots`
    risk_per_trade: float = 0.0
    lots: float = 0.01
    contract_size: float = 100.0
    max_lots: float = 1.0

    #: stop trading for the session once cumulative loss reaches this
    daily_loss_limit: float = 0.0
    #: do not re-enter the direction that just stopped out
    block_reentry_after_stop: bool = True
    heartbeat_timeout_ms: float = 5_000.0
    #: set False only for replay; live must be True
    require_fresh_quotes: bool = True
    max_quote_age_ms: float = 2_000.0


@dataclass
class RunnerState:
    position: ManagedPosition | None = None
    realised: float = 0.0
    trades: list[dict] = field(default_factory=list)
    #: sticky halt -- daily loss, feed death, position mismatch. Only
    #: reset_day() or operator intervention clears these.
    halted: bool = False
    halt_reason: str | None = None
    #: transient halt -- basis spike, event window, toxic flow. These are
    #: CONDITIONS, not faults: they clear themselves when the condition passes.
    #: Conflating the two permanently disables the system on a passing spike.
    gated: bool = False
    gate_reason: str | None = None
    last_stop_dir: int = 0
    n_signals: int = 0
    n_blocked: int = 0

    @property
    def blocked(self) -> bool:
        return self.halted or self.gated

    @property
    def block_reason(self) -> str | None:
        return self.halt_reason or self.gate_reason


class Runner:
    """Drive the system. Works identically over a replay and a live feed.

    Live wiring is four callables, so adapting to any broker API is a short
    file with nothing hidden between your code and the venue:

        runner = Runner(cfg, broker=my_broker)
        for pkt in runner.run(fut_feed, spot_feed):
            log(pkt)
    """

    def __init__(self, cfg: RunnerConfig | None = None, broker: Broker | None = None,
                 now_ns: Callable[[], int] = time.time_ns):
        self.cfg = cfg or RunnerConfig()
        self.broker = broker
        self.now_ns = now_ns
        self.engine = SignalEngine(self.cfg.engine)
        self.heartbeat = Heartbeat(self.cfg.heartbeat_timeout_ms)
        self.reconciler = PositionReconciler()
        self.state = RunnerState()

    # -- the loop --------------------------------------------------------
    def on_spot_quote(self, q: Quote) -> ExitReason | None:
        """STEP 1. Manage the open position. Call this on EVERY spot quote."""
        self.heartbeat.beat()
        self.engine.on_spot_quote(q)
        if self.broker is not None and hasattr(self.broker, "on_quote"):
            self.broker.on_quote(q)

        st = self.state
        if st.position is None or st.position.closed:
            return None

        reason = st.position.update(q, flatten=st.blocked)
        if reason == ExitReason.NONE:
            return None

        self._exit(st.position, reason)
        return reason

    def on_fut_quote(self, q: Quote) -> SignalPacket | None:
        """STEPS 2 and 3. Risk gates, then entry."""
        pkt = self.engine.on_fut_quote(q)
        if pkt is None:
            return None

        st = self.state
        cfg = self.cfg

        # --- sticky faults: these do NOT clear on their own ---------------
        if cfg.daily_loss_limit and st.realised <= -abs(cfg.daily_loss_limit):
            self._halt("daily_loss_limit")
        if not self.heartbeat.is_alive():
            self._halt(f"feed_stale_{self.heartbeat.age_ms():.0f}ms")

        # --- transient conditions: these track the engine, both ways ------
        # A basis spike or an event window is a condition that passes. Making
        # it sticky would disable the system for the rest of the session on a
        # momentary print, with no path back short of a restart.
        st.gated = bool(pkt.halt)
        st.gate_reason = pkt.halt_reason if pkt.halt else None

        if st.blocked:
            if st.position is not None and not st.position.closed:
                # flatten on the next quote we see; do not wait for a signal
                sq = self.engine._spot
                if sq is not None:
                    st.position.update(sq, flatten=True)
                    self._exit(st.position, ExitReason.SIGNAL)
            return pkt

        # --- entry -------------------------------------------------------
        if pkt.ofi_z is None or abs(pkt.ofi_z) < cfg.entry_z:
            return pkt
        st.n_signals += 1

        if st.position is not None and not st.position.closed:
            st.n_blocked += 1
            return pkt

        side = 1 if pkt.ofi_z > 0 else -1
        if cfg.block_reentry_after_stop and side == st.last_stop_dir:
            st.n_blocked += 1
            return pkt

        sq = self.engine._spot
        if sq is None:
            st.n_blocked += 1
            return pkt
        if cfg.require_fresh_quotes:
            age_ms = (self.now_ns() - sq.ts_recv) / NS_PER_MS
            if age_ms > cfg.max_quote_age_ms:
                st.n_blocked += 1
                return pkt

        lots = cfg.lots
        if cfg.risk_per_trade > 0:
            lots = size_for_risk(cfg.risk_per_trade, cfg.bracket.sl,
                                 cfg.contract_size, cfg.max_lots)
        if lots <= 0:
            st.n_blocked += 1
            return pkt

        self._enter(side, lots, sq)
        return pkt

    # -- entry / exit ----------------------------------------------------
    def _enter(self, side: int, lots: float, sq: Quote) -> None:
        entry_px = sq.ask_px if side > 0 else sq.bid_px

        if self.broker is not None:
            ex: Execution = self.broker.submit(
                Order(ts_ns=sq.ts_recv, lots=side * lots,
                      symbol=self.cfg.symbol, tag="entry"))
            if not ex.ok:
                # A rejection is information, not an error to retry through.
                # Retrying into a last-look venue is how you get filled only
                # on the orders that were about to go against you.
                self.state.n_blocked += 1
                return
            entry_px = ex.fill_px

        self.state.position = ManagedPosition(
            side=side, lots=lots, entry_px=entry_px,
            entry_ts=sq.ts_recv, cfg=self.cfg.bracket)

    def _exit(self, pos: ManagedPosition, reason: ExitReason) -> None:
        if self.broker is not None:
            self.broker.submit(Order(ts_ns=pos.exit_ts, lots=-pos.side * pos.lots,
                                     symbol=self.cfg.symbol,
                                     tag=f"exit_{reason.value}"))
            local = 0.0
            remote = self.broker.position(self.cfg.symbol)
            ok, msg = self.reconciler.check(local, remote)
            if not ok:
                self._halt(msg or "position_mismatch")

        pnl = pos.realised()
        self.state.realised += pnl
        self.state.trades.append({
            "entry_ts": pos.entry_ts, "exit_ts": pos.exit_ts,
            "side": pos.side, "lots": pos.lots,
            "entry_px": round(pos.entry_px, 4), "exit_px": round(pos.exit_px, 4),
            "reason": reason.value, "pnl": round(pnl, 4),
            "hold_s": round((pos.exit_ts - pos.entry_ts) / NS_PER_S, 1),
        })
        if reason in (ExitReason.STOP, ExitReason.TRAIL):
            self.state.last_stop_dir = pos.side
        else:
            self.state.last_stop_dir = 0
        self.state.position = None

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason

    def reset_day(self) -> None:
        """Call at the session boundary. Does NOT clear a position mismatch."""
        self.state.realised = 0.0
        self.state.last_stop_dir = 0
        if self.state.halt_reason and "mismatch" not in self.state.halt_reason:
            self.state.halted, self.state.halt_reason = False, None

    # -- replay driver ---------------------------------------------------
    def run(self, fut_quotes, spot_quotes) -> Iterator[SignalPacket]:
        """Replay driver. Merges on ts_recv, spot quotes managed first."""
        spot = list(spot_quotes)
        spot_ts = np.fromiter((q.ts_recv for q in spot), dtype="int64",
                              count=len(spot))
        cursor = 0

        for fq in fut_quotes:
            if isinstance(fq, Trade):
                self.engine.on_trade(fq)
                continue
            if not isinstance(fq, Quote):
                continue

            j = int(np.searchsorted(spot_ts, fq.ts_recv, side="right"))
            while cursor < j:
                self.on_spot_quote(spot[cursor])
                cursor += 1

            pkt = self.on_fut_quote(fq)
            if pkt is not None:
                yield pkt

        while cursor < len(spot):
            self.on_spot_quote(spot[cursor])
            cursor += 1

    def summary(self) -> dict:
        st = self.state
        n = len(st.trades)
        wins = sum(1 for t in st.trades if t["pnl"] > 0)
        by: dict[str, int] = {}
        for t in st.trades:
            by[t["reason"]] = by.get(t["reason"], 0) + 1
        return {
            "signals": st.n_signals,
            "blocked": st.n_blocked,
            "trades": n,
            "realised": round(st.realised, 4),
            "win_rate": round(wins / n, 3) if n else 0.0,
            "exits": by,
            "halted": st.halted,
            "halt_reason": st.halt_reason,
            "gated": st.gated,
            "gate_reason": st.gate_reason,
        }
