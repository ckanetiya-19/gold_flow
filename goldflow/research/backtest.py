"""
Event-driven, strictly causal backtester for the split architecture:
signals from COMEX GC, execution on broker XAUUSD.

Why this file exists at all: the MT5 Strategy Tester cannot do this. It
replays one symbol's ticks and has no mechanism to replay a second
instrument's order-book event stream at nanosecond resolution alongside it.
Anything the tester says about a two-leg system is fiction.

Causality is enforced structurally, not by discipline:

  * All events are merged on ts_recv -- what had ARRIVED, not what had
    HAPPENED. Merging on exchange time backtests a machine you do not own.
  * A signal computed at t cannot be acted on before t + latency_ns.
  * Orders fill at the NEXT spot quote strictly after the decision time, at
    the far side of the spread plus modelled slippage. Never at the mid, never
    at the quote that triggered the decision.
  * The rolling normaliser inside RollingOFIZScore uses only past buckets.

`lookahead_probe` re-runs the whole thing with the signal deliberately delayed
by one extra bucket. A strategy whose performance collapses under that is not
robust; a strategy whose performance IMPROVES has look-ahead somewhere and the
result is void.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

import numpy as np

from ..core.ofi import OFIBucket, OFIBucketer, RollingOFIZScore
from ..core.sessions import SessionClock
from ..feeds.base import NS_PER_MS, NS_PER_S, Quote, Trade
from .costs import CostConfig, SpreadModel


# ---------------------------------------------------------------------------
# Signal state handed to a strategy
# ---------------------------------------------------------------------------

@dataclass
class SignalState:
    ts_ns: int
    ofi_z: float | None
    ofi_normalised: float
    avg_depth: float
    fut_mid: float
    spot_bid: float
    spot_ask: float
    basis: float
    basis_z: float | None
    event_window: str | None
    vpin: float | None = None
    vpin_toxic: bool = False

    @property
    def spot_mid(self) -> float:
        return 0.5 * (self.spot_bid + self.spot_ask)


Strategy = Callable[[SignalState, float], float]
"""(state, current_position) -> target position in lots. Pure function."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    ts_ns: int
    lots: float
    price: float
    cost: float


@dataclass
class BacktestResult:
    equity_ts: np.ndarray
    equity: np.ndarray
    fills: list[Fill]
    gross_pnl: float
    total_cost: float
    n_events: int
    signal_delay_ns: int

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_cost

    @property
    def n_trades(self) -> int:
        return len(self.fills)

    @property
    def cost_ratio(self) -> float:
        return self.total_cost / abs(self.gross_pnl) if self.gross_pnl else np.inf

    def sharpe(self, periods_per_year: float | None = None) -> float:
        """Annualised Sharpe on the marked-to-market equity curve.

        Sampled per bucket, so with 1-second buckets the annualisation factor
        is ~4,500 and the number gets large fast in both directions. That is
        arithmetically correct and practically useless as a headline: most of
        the sampled periods are flat, which understates the variance of the
        thing you actually hold. `sharpe_per_trade` is the honest companion --
        report both, and treat |Sharpe| above ~10 as a signal that the
        sampling, not the strategy, is doing the work.
        """
        if self.equity.size < 30:
            return 0.0
        r = np.diff(self.equity)
        sd = r.std(ddof=1)
        if sd <= 0:
            return 0.0
        if periods_per_year is None:
            span_s = max((self.equity_ts[-1] - self.equity_ts[0]) / NS_PER_S, 1.0)
            per_s = self.equity.size / span_s
            periods_per_year = per_s * 3600 * 23 * 252
        return float(r.mean() / sd * np.sqrt(periods_per_year))

    def sharpe_per_trade(self) -> float:
        """Mean net PnL per trade divided by its standard deviation.

        No annualisation, no assumption about sampling frequency. This is the
        number to compare across configurations.
        """
        if len(self.fills) < 5:
            return 0.0
        pnl = np.diff(self.equity) if self.equity.size > 1 else np.zeros(0)
        if pnl.size < 5:
            return 0.0
        active = pnl[pnl != 0.0]
        if active.size < 5 or active.std(ddof=1) <= 0:
            return 0.0
        return float(active.mean() / active.std(ddof=1))

    def max_drawdown(self) -> float:
        if self.equity.size == 0:
            return 0.0
        peak = np.maximum.accumulate(self.equity)
        return float((peak - self.equity).max())

    def summary(self) -> dict:
        return {
            "gross_pnl": round(self.gross_pnl, 2),
            "total_cost": round(self.total_cost, 2),
            "net_pnl": round(self.net_pnl, 2),
            "n_trades": self.n_trades,
            "cost_as_pct_of_gross": (round(100 * self.cost_ratio, 1)
                                     if np.isfinite(self.cost_ratio) else None),
            "sharpe": round(self.sharpe(), 2),
            "sharpe_per_trade": round(self.sharpe_per_trade(), 3),
            "max_drawdown": round(self.max_drawdown(), 2),
            "events": self.n_events,
            "signal_delay_ms": self.signal_delay_ns / NS_PER_MS,
        }

    def __str__(self) -> str:
        s = self.summary()
        verdict = "NET LOSS" if self.net_pnl <= 0 else "net positive"
        return (
            f"Backtest  {verdict}\n"
            f"  gross {s['gross_pnl']:+,.2f}   costs {s['total_cost']:,.2f}"
            f"   net {s['net_pnl']:+,.2f}\n"
            f"  trades {s['n_trades']}   costs are {s['cost_as_pct_of_gross']}% of gross\n"
            f"  Sharpe/trade {s['sharpe_per_trade']}   (annualised {s['sharpe']}"
            f" -- see docstring)   maxDD {s['max_drawdown']:,.2f}\n"
            f"  signal delay {s['signal_delay_ms']:g}ms"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    bucket_ns: int = 1 * NS_PER_S
    #: compute + network latency between the signal closing and an order landing
    signal_delay_ns: int = 250 * NS_PER_MS
    zscore_window: int = 600
    zscore_min_obs: int = 60
    #: contract size ratio; 1 GC (100oz) == 1 standard XAUUSD lot at most brokers
    lots_per_unit: float = 1.0
    #: stand aside inside scheduled event windows
    skip_event_windows: bool = True
    #: |basis z| above this halts trading (the EFP kill switch)
    basis_z_halt: float = 4.0
    basis_window: int = 3_600
    costs: CostConfig = field(default_factory=CostConfig)


class Backtester:
    def __init__(self, cfg: BacktestConfig | None = None):
        self.cfg = cfg or BacktestConfig()

    def run(self, fut_quotes: Sequence[Quote], spot_quotes: Sequence[Quote],
            strategy: Strategy, extra_delay_ns: int = 0) -> BacktestResult:
        cfg = self.cfg
        delay = cfg.signal_delay_ns + extra_delay_ns

        bucketer = OFIBucketer(cfg.bucket_ns)
        zscorer = RollingOFIZScore(cfg.zscore_window, cfg.zscore_min_obs)
        clock = SessionClock()
        spreads = SpreadModel(cfg.costs)

        spot_ts = np.fromiter((q.ts_recv for q in spot_quotes),
                              dtype="int64", count=len(spot_quotes))

        basis_hist: list[float] = []
        position = 0.0
        entry_px = 0.0
        gross = 0.0
        cost_total = 0.0
        fills: list[Fill] = []
        eq_ts: list[int] = []
        eq: list[float] = []
        n_events = 0

        def spot_at(ts: int) -> tuple[int, Quote] | None:
            """First spot quote STRICTLY AFTER ts. This is the fill quote."""
            i = int(np.searchsorted(spot_ts, ts, side="right"))
            if i >= len(spot_quotes):
                return None
            return i, spot_quotes[i]

        for q in fut_quotes:
            n_events += 1
            bucket = bucketer.update(q)
            if bucket is None:
                continue

            decide_ts = bucket.ts_end + delay
            got = spot_at(decide_ts)
            if got is None:
                break
            _, sq = got

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

            state = SignalState(
                ts_ns=decide_ts,
                ofi_z=z,
                ofi_normalised=bucket.normalised,
                avg_depth=bucket.avg_depth,
                fut_mid=bucket.mid_end,
                spot_bid=sq.bid_px,
                spot_ask=sq.ask_px,
                basis=basis,
                basis_z=bz,
                event_window=clock.current_event,
            )

            # --- risk gates, applied before the strategy is consulted -----
            halted = (
                (cfg.skip_event_windows and state.event_window is not None)
                or (bz is not None and abs(bz) > cfg.basis_z_halt)
            )
            target = 0.0 if halted else float(strategy(state, position))
            target = float(np.clip(target, -10.0, 10.0))

            if abs(target - position) > 1e-9:
                # mark the closing leg before changing position
                if position != 0.0:
                    gross += position * (sq.mid - entry_px)
                traded = abs(target - position)
                # cross the spread in the direction of the trade
                fill_px = sq.ask_px if target > position else sq.bid_px
                c = spreads.cost_per_side(decide_ts, traded)
                cost_total += c
                fills.append(Fill(decide_ts, target - position, fill_px, c))
                position = target
                entry_px = sq.mid

            mtm = gross + (position * (sq.mid - entry_px) if position else 0.0)
            eq_ts.append(decide_ts)
            eq.append(mtm - cost_total)

        # close out at the end
        if position != 0.0 and spot_quotes:
            last = spot_quotes[-1]
            gross += position * (last.mid - entry_px)
            c = spreads.cost_per_side(last.ts_recv, abs(position))
            cost_total += c
            fills.append(Fill(last.ts_recv, -position, last.mid, c))

        return BacktestResult(
            equity_ts=np.asarray(eq_ts, dtype="int64"),
            equity=np.asarray(eq, dtype=float),
            fills=fills,
            gross_pnl=gross,
            total_cost=cost_total,
            n_events=n_events,
            signal_delay_ns=delay,
        )

    # -- diagnostics -----------------------------------------------------
    def lookahead_probe(self, fut: Sequence[Quote], spot: Sequence[Quote],
                        strategy: Strategy) -> dict:
        """Re-run with one extra bucket of delay. Interpret the delta honestly.

        Expected: net PnL degrades. If it degrades to nothing, the edge lives
        entirely inside one bucket and will not survive real latency. If it
        IMPROVES, there is look-ahead in the pipeline -- find it before you
        read another number off this backtester.
        """
        base = self.run(fut, spot, strategy)
        delayed = self.run(fut, spot, strategy,
                           extra_delay_ns=self.cfg.bucket_ns)
        d_net = delayed.net_pnl - base.net_pnl

        # The comparison is only meaningful when the baseline actually makes
        # money. A losing strategy that loses LESS when delayed has not
        # revealed look-ahead -- it has revealed that trading less is better,
        # which is a statement about the edge, not about causality. Reporting
        # "look-ahead suspected" there would be a false alarm.
        if base.net_pnl <= 0:
            verdict = ("inconclusive -- baseline is not profitable, so the "
                       "probe cannot distinguish look-ahead from noise")
        elif d_net > 0:
            verdict = "LOOK-AHEAD SUSPECTED (delay improved a profitable baseline)"
        elif base.net_pnl > 0 and delayed.net_pnl <= 0:
            verdict = ("edge lives entirely inside one bucket -- will not "
                       "survive real latency")
        else:
            verdict = "degrades under delay, as expected"

        return {
            "base_net": round(base.net_pnl, 2),
            "delayed_net": round(delayed.net_pnl, 2),
            "delta": round(d_net, 2),
            "base_gross_per_trade": (round(base.gross_pnl / base.n_trades, 4)
                                     if base.n_trades else 0.0),
            "verdict": verdict,
        }


# ---------------------------------------------------------------------------
# Reference strategies
# ---------------------------------------------------------------------------

def ofi_threshold_strategy(entry_z: float = 2.0, exit_z: float = 0.5,
                           size: float = 1.0) -> Strategy:
    """Trade in the direction of depth-normalised OFI beyond a z threshold.

    The most literal implementation of the Cont-Kukanov-Stoikov result. It is
    the honest baseline: if this does not work on your data, more elaborate
    signals built on the same input will not either.
    """
    def strat(s: SignalState, pos: float) -> float:
        if s.ofi_z is None:
            return 0.0
        if pos == 0.0:
            if s.ofi_z >= entry_z:
                return size
            if s.ofi_z <= -entry_z:
                return -size
            return 0.0
        if abs(s.ofi_z) < exit_z:
            return 0.0
        if pos > 0 and s.ofi_z <= -entry_z:
            return -size
        if pos < 0 and s.ofi_z >= entry_z:
            return size
        return pos
    return strat


class OFITimedStrategy:
    """Enter on a z-threshold, hold for a FIXED TIME, then exit.

    Exists because of a specific result: Experiment 4 typically shows the
    favourable move accumulating over tens of seconds, while
    `ofi_threshold_strategy` exits as soon as |z| decays below exit_z -- often
    within a bucket or two. That exit rule pays the spread to capture the part
    of the move that has not happened yet.

    Separating the holding period from the signal decay makes the two testable
    independently, which is the only way to tell "no edge" apart from "edge,
    wrong exit". Set `hold_s` from Experiment 4's best horizon, not by search.
    """

    def __init__(self, entry_z: float = 2.0, hold_s: float = 30.0,
                 size: float = 1.0, stop_move: float | None = None):
        self.entry_z = entry_z
        self.hold_ns = int(hold_s * NS_PER_S)
        self.size = size
        self.stop_move = stop_move
        self._entry_ts: int | None = None
        self._entry_px: float | None = None

    def reset(self) -> None:
        self._entry_ts = None
        self._entry_px = None

    def __call__(self, s: SignalState, pos: float) -> float:
        if pos != 0.0 and self._entry_ts is not None:
            held = s.ts_ns - self._entry_ts
            if self.stop_move is not None and self._entry_px is not None:
                adverse = (self._entry_px - s.spot_mid) * np.sign(pos)
                if adverse >= self.stop_move:
                    self.reset()
                    return 0.0
            if held >= self.hold_ns:
                self.reset()
                return 0.0
            return pos

        if s.ofi_z is None or abs(s.ofi_z) < self.entry_z:
            return 0.0
        self._entry_ts = s.ts_ns
        self._entry_px = s.spot_mid
        return self.size if s.ofi_z > 0 else -self.size


def flat_strategy() -> Strategy:
    """Never trades. The control. Its net PnL must be exactly zero -- if it is
    not, the accounting in the engine is wrong and every other number is too."""
    return lambda s, pos: 0.0
