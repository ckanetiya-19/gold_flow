"""
Bracket-aware backtester -- the one that matches what you will actually run.

The plain `Backtester` only looks at the spot book at decision points, which
is correct for a target-position strategy and WRONG the moment you add TP/SL.
A stop that is only checked once a second is not the stop your broker will
execute; it will be hit between your checks, at a worse price, and a backtest
that misses those exits reports a strategy that does not exist.

So this engine walks EVERY spot quote between decisions and feeds each one to
the same `ManagedPosition` object the live runner uses. Same state machine,
same tie-breaking, same spread-side arithmetic.

Causality is preserved exactly as in the plain backtester:
  * decisions land at bucket_end + latency, never earlier;
  * entries fill at the far side of the book on the first quote strictly after
    the decision;
  * exits fill at the far side on the quote that triggered them;
  * ties between stop and target resolve to the STOP.

`intrabar_note` in the result records the one approximation that remains: with
quote data rather than full tick data, a stop and a target inside the SAME
quote's move are unresolvable, and this engine takes the stop. That makes the
result pessimistic; a real fill could have been either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

from ..core.ofi import OFIBucketer, RollingOFIZScore
from ..core.sessions import SessionClock
from ..feeds.base import NS_PER_MS, NS_PER_S, Quote
from ..live.position import BracketConfig, ExitReason, ManagedPosition, size_for_risk
from .backtest import SignalState
from .costs import CostConfig, SpreadModel


@dataclass
class BracketTrade:
    entry_ts: int
    exit_ts: int
    side: int
    lots: float
    entry_px: float
    exit_px: float
    reason: ExitReason
    gross: float
    cost: float

    @property
    def net(self) -> float:
        return self.gross - self.cost

    @property
    def hold_s(self) -> float:
        return (self.exit_ts - self.entry_ts) / NS_PER_S


@dataclass
class BracketResult:
    trades: list[BracketTrade]
    equity_ts: np.ndarray
    equity: np.ndarray
    n_signals: int
    n_blocked: int
    intrabar_note: str = (
        "Stop wins stop/target ties within one quote -- pessimistic by design."
    )

    @property
    def net(self) -> float:
        return sum(t.net for t in self.trades)

    @property
    def gross(self) -> float:
        return sum(t.gross for t in self.trades)

    @property
    def costs(self) -> float:
        return sum(t.cost for t in self.trades)

    def by_reason(self) -> dict:
        out: dict[str, dict] = {}
        for t in self.trades:
            d = out.setdefault(t.reason.value or "none",
                               {"n": 0, "net": 0.0, "mean_hold_s": 0.0})
            d["n"] += 1
            d["net"] += t.net
            d["mean_hold_s"] += t.hold_s
        for d in out.values():
            d["net"] = round(d["net"], 3)
            d["mean_hold_s"] = round(d["mean_hold_s"] / d["n"], 1)
        return out

    def summary(self) -> dict:
        n = len(self.trades)
        wins = [t for t in self.trades if t.net > 0]
        return {
            "signals": self.n_signals,
            "blocked_by_risk": self.n_blocked,
            "trades": n,
            "gross": round(self.gross, 2),
            "costs": round(self.costs, 2),
            "net": round(self.net, 2),
            "win_rate": round(len(wins) / n, 3) if n else 0.0,
            "expectancy_per_trade": round(self.net / n, 4) if n else 0.0,
            "exits": self.by_reason(),
        }

    def __str__(self) -> str:
        s = self.summary()
        verdict = "NET LOSS" if self.net <= 0 else "net positive"
        lines = [
            f"Bracket backtest  {verdict}",
            f"  signals {s['signals']}  blocked {s['blocked_by_risk']}  "
            f"trades {s['trades']}",
            f"  gross {s['gross']:+,.2f}   costs {s['costs']:,.2f}   "
            f"net {s['net']:+,.2f}",
            f"  win rate {s['win_rate']:.1%}   "
            f"expectancy/trade {s['expectancy_per_trade']:+.4f}",
            "  exits:",
        ]
        for k, v in s["exits"].items():
            lines.append(f"    {k:<8} n={v['n']:<5} net={v['net']:+9.3f}  "
                         f"mean hold {v['mean_hold_s']:.0f}s")
        lines.append(f"  {self.intrabar_note}")
        return "\n".join(lines)


@dataclass
class BracketBacktestConfig:
    bucket_ns: int = NS_PER_S
    signal_delay_ns: int = 250 * NS_PER_MS
    zscore_window: int = 600
    zscore_min_obs: int = 60
    entry_z: float = 2.0
    bracket: BracketConfig = field(default_factory=BracketConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    skip_event_windows: bool = True
    basis_z_halt: float = 4.0
    basis_window: int = 3_600
    #: currency risked per trade; 0 uses fixed `lots`
    risk_per_trade: float = 0.0
    lots: float = 1.0
    contract_size: float = 100.0
    max_lots: float = 1.0
    #: one position at a time. Pyramiding needs its own study.
    allow_reentry_same_direction: bool = False


class BracketBacktester:
    def __init__(self, cfg: BracketBacktestConfig | None = None):
        self.cfg = cfg or BracketBacktestConfig()

    def run(self, fut_quotes: Sequence[Quote], spot_quotes: Sequence[Quote]
            ) -> BracketResult:
        cfg = self.cfg
        bucketer = OFIBucketer(cfg.bucket_ns)
        zscorer = RollingOFIZScore(cfg.zscore_window, cfg.zscore_min_obs)
        clock = SessionClock()
        spreads = SpreadModel(cfg.costs)

        spot_ts = np.fromiter((q.ts_recv for q in spot_quotes),
                              dtype="int64", count=len(spot_quotes))

        pos: ManagedPosition | None = None
        trades: list[BracketTrade] = []
        basis_hist: list[float] = []
        eq_ts: list[int] = []
        eq: list[float] = []
        realised = 0.0
        n_signals = n_blocked = 0
        cursor = 0                       # index into spot, never rewinds
        last_dir = 0

        for q in fut_quotes:
            bucket = bucketer.update(q)
            if bucket is None:
                continue

            decide_ts = bucket.ts_end + cfg.signal_delay_ns
            j = int(np.searchsorted(spot_ts, decide_ts, side="right"))
            if j >= len(spot_quotes):
                break

            # ---- walk EVERY spot quote up to the decision, managing the
            # ---- open position. This is the whole reason this class exists.
            while cursor < j:
                sq = spot_quotes[cursor]
                if pos is not None and not pos.closed:
                    if pos.update(sq) != ExitReason.NONE:
                        t = self._close_trade(pos, spreads)
                        trades.append(t)
                        realised += t.net
                        last_dir = pos.side
                        pos = None
                cursor += 1

            sq = spot_quotes[j]
            clock.update(decide_ts)

            basis = bucket.mid_end - sq.mid
            basis_hist.append(basis)
            if len(basis_hist) > cfg.basis_window:
                basis_hist.pop(0)
            bz = None
            if len(basis_hist) >= 200:
                arr = np.asarray(basis_hist[:-1])
                sd = float(arr.std(ddof=1))
                bz = (basis - float(arr.mean())) / sd if sd > 1e-9 else 0.0

            z = zscorer.update(bucket)

            halted = (
                (cfg.skip_event_windows and clock.current_event is not None)
                or (bz is not None and abs(bz) > cfg.basis_z_halt)
            )

            # risk gate flattens an open position rather than just blocking
            if halted and pos is not None and not pos.closed:
                pos.update(sq, flatten=True)
                t = self._close_trade(pos, spreads)
                trades.append(t)
                realised += t.net
                pos = None

            if z is not None and abs(z) >= cfg.entry_z:
                n_signals += 1
                side = 1 if z > 0 else -1
                if halted:
                    n_blocked += 1
                elif pos is not None:
                    pass                                 # already in a trade
                elif (not cfg.allow_reentry_same_direction
                      and side == last_dir and trades
                      and trades[-1].reason == ExitReason.STOP):
                    # do not immediately re-enter the direction that just
                    # stopped you out -- that is how one bad regime becomes
                    # ten identical losses
                    n_blocked += 1
                else:
                    lots = cfg.lots
                    if cfg.risk_per_trade > 0:
                        lots = size_for_risk(cfg.risk_per_trade,
                                             cfg.bracket.sl,
                                             cfg.contract_size, cfg.max_lots)
                    if lots > 0:
                        entry_px = sq.ask_px if side > 0 else sq.bid_px
                        slip = (spreads.half_spread(decide_ts)
                                * cfg.costs.slippage_mult)
                        entry_px += slip if side > 0 else -slip
                        pos = ManagedPosition(side=side, lots=lots,
                                              entry_px=entry_px,
                                              entry_ts=sq.ts_recv,
                                              cfg=cfg.bracket)

            mtm = realised + (pos.unrealised(sq) if pos and not pos.closed else 0.0)
            eq_ts.append(decide_ts)
            eq.append(mtm)

        # flush any open position at the last quote
        if pos is not None and not pos.closed and spot_quotes:
            last = spot_quotes[-1]
            pos.update(last, flatten=True)
            t = self._close_trade(pos, spreads)
            trades.append(t)

        return BracketResult(
            trades=trades,
            equity_ts=np.asarray(eq_ts, dtype="int64"),
            equity=np.asarray(eq, dtype=float),
            n_signals=n_signals,
            n_blocked=n_blocked,
        )

    def _close_trade(self, pos: ManagedPosition,
                     spreads: SpreadModel) -> BracketTrade:
        gross = pos.realised()
        cost = (spreads.cost_per_side(pos.entry_ts, pos.lots)
                + spreads.cost_per_side(pos.exit_ts, pos.lots))
        return BracketTrade(
            entry_ts=pos.entry_ts, exit_ts=pos.exit_ts, side=pos.side,
            lots=pos.lots, entry_px=pos.entry_px, exit_px=pos.exit_px,
            reason=pos.exit_reason, gross=gross, cost=cost,
        )
