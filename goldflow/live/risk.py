"""
Risk gates. These run BEFORE the strategy is consulted, not after.

Ordering matters: a strategy that is asked for a target and then has it
overridden will still have updated its internal state as if it had traded.
Gating first means the strategy never sees a bar it was not allowed to act on.

The gates, and why each exists:

  stale_data     The signal is older than `max_signal_age_ms`. A frozen feed
                 that keeps repeating its last instruction is the single most
                 dangerous failure mode in a bridged system. Degrade to FLAT,
                 never to "hold last".

  basis_blowout  |basis z| beyond threshold. This is the EFP kill switch. The
                 gold basis blew out to roughly $50/oz in early 2025 on tariff
                 concerns; a futures-signal/spot-execution system is short that
                 dislocation and must be flat through it.

  event_window   Scheduled liquidity events -- the London fixes, US macro at
                 08:30 ET. Spreads triple exactly when flow signals look
                 strongest, which is how a backtest with a constant spread
                 finds a fictional edge.

  toxic_flow     VPIN in the top decile of its own history. Not a directional
                 call; the market makers are being adversely selected and the
                 book is about to thin. Stand aside.

  spread_guard   The spot spread itself is wide right now. Cheap, direct, and
                 catches conditions the scheduled windows miss.

  daily_loss     Hard stop. Non-negotiable and not overridable by the strategy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..feeds.base import NS_PER_MS, Quote


@dataclass
class RiskConfig:
    max_signal_age_ms: float = 2_000.0
    basis_z_halt: float = 4.0
    halt_in_event_windows: bool = True
    halt_on_toxic_flow: bool = True
    max_spot_spread: float = 0.80          # price units; widen after measuring
    max_daily_loss: float = 0.0            # 0 disables
    max_position: float = 3.0


class RiskGate:
    def __init__(self, cfg: RiskConfig | None = None):
        self.cfg = cfg or RiskConfig()
        self.daily_pnl = 0.0
        self.tripped: str | None = None

    def record_pnl(self, delta: float) -> None:
        self.daily_pnl += delta

    def reset_day(self) -> None:
        self.daily_pnl = 0.0
        self.tripped = None

    def check(self, ts_ns: int, spot: Quote | None,
              basis_z: float | None, event_window: str | None,
              vpin_toxic: bool, now_ns: int | None = None
              ) -> tuple[bool, str | None]:
        """Returns (halt, reason). A tripped daily loss stays tripped."""
        c = self.cfg

        if self.tripped:
            return True, self.tripped

        if c.max_daily_loss and self.daily_pnl <= -abs(c.max_daily_loss):
            self.tripped = "daily_loss_limit"
            return True, self.tripped

        if spot is None:
            return True, "no_spot_quote"

        now = now_ns if now_ns is not None else ts_ns
        age_ms = (now - spot.ts_recv) / NS_PER_MS
        if age_ms > c.max_signal_age_ms:
            return True, f"stale_spot_{age_ms:.0f}ms"

        if spot.is_crossed():
            return True, "crossed_spot_book"

        if spot.spread > c.max_spot_spread:
            return True, f"wide_spread_{spot.spread:.2f}"

        if basis_z is not None and abs(basis_z) > c.basis_z_halt:
            return True, f"basis_blowout_z{basis_z:+.1f}"

        if c.halt_in_event_windows and event_window is not None:
            return True, f"event_{event_window}"

        if c.halt_on_toxic_flow and vpin_toxic:
            return True, "toxic_flow"

        return False, None


class Heartbeat:
    """Liveness watchdog for the feed itself.

    Distinct from staleness of a single quote: this catches the case where the
    whole process is still running and still emitting packets, but the upstream
    connection died and nothing new is arriving. Poll `is_alive()` from a
    supervisor, not from the trading loop -- a loop that has stopped receiving
    events will also have stopped checking.
    """

    def __init__(self, timeout_ms: float = 5_000.0):
        self.timeout_ms = timeout_ms
        self._last = time.time_ns()

    def beat(self) -> None:
        self._last = time.time_ns()

    def is_alive(self) -> bool:
        return (time.time_ns() - self._last) / NS_PER_MS < self.timeout_ms

    def age_ms(self) -> float:
        return (time.time_ns() - self._last) / NS_PER_MS
