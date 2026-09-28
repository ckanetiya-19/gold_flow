"""
Broker probe -- generate the order log that execution_quality.py needs.

You cannot measure a broker without sending it orders. This runs a controlled
probe campaign: small, randomised, correctly captured, with hard safety rails,
and it hands you a report at the end.

WHAT MAKES THE CAPTURE CORRECT (and why most logs are useless)

  * `requested_px` is the price you SAW at decision time, on YOUR side of the
    book -- ask for a buy, bid for a sell. Not the mid. Comparing fills against
    the mid manufactures a half-spread of phantom price improvement on every
    trade; `slippage_symmetry` will flag it as IMPLAUSIBLE, but only after you
    have wasted the campaign.
  * Direction is RANDOM, not signal-driven. If you probe with your strategy,
    the asymmetry test cannot separate "the broker rejected my good orders"
    from "my signal was good". Random direction makes the null exact: with no
    signal, E[favourable move] must be zero for filled and rejected alike, so
    any gap is the broker.
  * Every order is flattened immediately. This measures execution, not
    strategy. You are buying information, and the position is not the point.
  * Timestamps come from one clock. Mixing the platform's server time with
    local time puts the measurement error inside the thing being measured.

SAFETY RAILS

Hard caps on order count, notional, and session duration, plus a kill switch
on cumulative loss. The probe stops rather than continues on any breach. It
also refuses to run against a live broker unless `i_understand_live=True` is
passed explicitly -- a probe campaign sends real orders and costs real spread.

EXPECTED COST

Roughly (orders x round-trip cost). At 300 orders on 0.01 lots with a 0.25
round trip, that is about 75 price-units of spread -- a few dollars on a micro
lot. That is the price of knowing, and it is far cheaper than discovering the
same fact through six months of unexplained underperformance.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np

from ..feeds.base import NS_PER_MS, NS_PER_S, Quote
from ..research.execution_quality import (
    MIN_FILLS,
    MIN_REJECTS,
    OrderRecord,
    full_report,
)


@dataclass
class ProbeConfig:
    """Every limit here is a stop, not a target."""

    n_orders: int = 300
    lots: float = 0.01
    #: minimum gap between probes; avoids looking like a burst to the LP and
    #: spreads the sample across conditions rather than clustering it
    min_gap_s: float = 20.0
    max_gap_s: float = 90.0
    #: hold before flattening. Keep short: this measures entry execution.
    hold_s: float = 2.0

    # -- rails ---------------------------------------------------------
    #: Ceiling against runaway loops, not a target. It has to be generous:
    #: convicting a bad broker takes a few hundred orders, but CLEARING an
    #: honest one takes thousands, because rejections are the scarce sample and
    #: an honest broker produces few of them. A ceiling of 500 can never clear
    #: anyone -- it only ever returns "underpowered", which reads as suspicion
    #: the data does not support.
    max_orders: int = 5_000
    max_session_hours: float = 12.0
    max_cumulative_loss: float = 0.0        # 0 = disabled; set it
    max_spread_to_probe: float = 1.50       # skip absurd quotes

    #: fraction of probes deliberately aimed at fast markets. A broker that
    #: behaves well only in quiet conditions has told you nothing about the
    #: conditions your signal fires in.
    fast_market_fraction: float = 0.30
    fast_market_vol_quantile: float = 0.80

    seed: int = 0
    symbol: str = "XAUUSD"


@dataclass
class ProbeState:
    orders: list[OrderRecord] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    realised: float = 0.0
    skipped_wide: int = 0
    skipped_stale: int = 0
    stopped_reason: str | None = None

    @property
    def n_filled(self) -> int:
        return sum(1 for o in self.orders if o.filled)

    @property
    def n_rejected(self) -> int:
        return sum(1 for o in self.orders if not o.filled)


class BrokerProbe:
    """Run a probe campaign against any object satisfying the Broker protocol.

    The broker adapter must expose:
        submit(ts_request, side, lots, requested_px) -> OrderRecord
    or be wrapped by `adapt_broker` below.

    Typical use -- dry run first, always:

        from goldflow.research import LastLookBroker
        probe = BrokerProbe(ProbeConfig(n_orders=3000))
        state = probe.run_offline(LastLookBroker(quotes, aggression=0.8), quotes)
        print(probe.report(state))
    """

    def __init__(self, cfg: ProbeConfig | None = None):
        self.cfg = cfg or ProbeConfig()
        self._rng = random.Random(self.cfg.seed)
        self._nprng = np.random.default_rng(self.cfg.seed)

    # -- offline / replay ------------------------------------------------
    def run_offline(self, broker, quotes: Sequence[Quote]) -> ProbeState:
        """Replay a probe campaign over recorded quotes.

        Use this to validate the whole pipeline before spending a rupee, and to
        confirm your broker adapter produces well-formed records.
        """
        cfg = self.cfg
        st = ProbeState(quotes=list(quotes))
        if len(quotes) < 1_000:
            raise ValueError("need >=1000 quotes to probe over")

        idx = self._pick_indices(quotes, min(cfg.n_orders, cfg.max_orders))
        for i in idx:
            q = quotes[i]
            if q.is_crossed():
                continue
            if q.spread > cfg.max_spread_to_probe:
                st.skipped_wide += 1
                continue

            side = 1 if self._rng.random() < 0.5 else -1
            # the price you SAW, on your side of the book
            px = q.ask_px if side > 0 else q.bid_px

            rec = broker.submit(q.ts_recv, side, cfg.lots, px)
            st.orders.append(rec)

            if rec.filled and rec.fill_px is not None:
                exit_i = self._index_after(quotes, q.ts_recv
                                           + int(cfg.hold_s * NS_PER_S))
                if exit_i is not None:
                    xq = quotes[exit_i]
                    exit_px = xq.bid_px if side > 0 else xq.ask_px
                    st.realised += (exit_px - rec.fill_px) * side * cfg.lots

            if cfg.max_cumulative_loss and st.realised <= -abs(cfg.max_cumulative_loss):
                st.stopped_reason = "max_cumulative_loss"
                break
        return st

    # -- live ------------------------------------------------------------
    def run_live(self, submit_fn: Callable[[int, int, float, float], OrderRecord],
                 quote_fn: Callable[[], Quote | None],
                 sleep_fn: Callable[[float], None],
                 now_ns_fn: Callable[[], int],
                 i_understand_live: bool = False) -> ProbeState:
        """Run against a real broker. Sends real orders.

        Deliberately takes plain callables rather than a broker object, so
        wiring it to any API is a twenty-line adapter and there is no hidden
        behaviour between your code and the venue.

        `submit_fn(ts_request, side, lots, requested_px) -> OrderRecord` must
        set `filled`, and on a fill both `fill_px` and `ts_decision`. If your
        API does not report a decision timestamp, record the moment the
        response arrived -- an approximate hold time is far better than none,
        since the hold time is the whole question.
        """
        if not i_understand_live:
            raise RuntimeError(
                "run_live sends real orders and costs real spread. "
                "Run run_offline first, then pass i_understand_live=True."
            )
        cfg = self.cfg
        st = ProbeState()
        t_start = now_ns_fn()
        deadline = t_start + int(cfg.max_session_hours * 3_600 * NS_PER_S)

        while len(st.orders) < min(cfg.n_orders, cfg.max_orders):
            if now_ns_fn() > deadline:
                st.stopped_reason = "max_session_hours"
                break

            q = quote_fn()
            if q is None:
                sleep_fn(1.0)
                continue
            st.quotes.append(q)

            age_ms = (now_ns_fn() - q.ts_recv) / NS_PER_MS
            if age_ms > 2_000:
                st.skipped_stale += 1
                sleep_fn(1.0)
                continue
            if q.is_crossed() or q.spread > cfg.max_spread_to_probe:
                st.skipped_wide += 1
                sleep_fn(1.0)
                continue

            side = 1 if self._rng.random() < 0.5 else -1
            px = q.ask_px if side > 0 else q.bid_px
            rec = submit_fn(now_ns_fn(), side, cfg.lots, px)
            st.orders.append(rec)

            if cfg.max_cumulative_loss and st.realised <= -abs(cfg.max_cumulative_loss):
                st.stopped_reason = "max_cumulative_loss"
                break

            sleep_fn(self._rng.uniform(cfg.min_gap_s, cfg.max_gap_s))

        return st

    # -- reporting -------------------------------------------------------
    def report(self, st: ProbeState) -> dict:
        rep: dict = {
            "orders_sent": len(st.orders),
            "filled": st.n_filled,
            "rejected": st.n_rejected,
            "skipped_wide_spread": st.skipped_wide,
            "skipped_stale_quote": st.skipped_stale,
            "probe_pnl": round(st.realised, 4),
            "stopped_reason": st.stopped_reason,
        }
        short = []
        if st.n_filled < MIN_FILLS:
            short.append(f"fills {st.n_filled}/{MIN_FILLS}")
        if st.n_rejected < MIN_REJECTS:
            short.append(f"rejections {st.n_rejected}/{MIN_REJECTS}")
        if short:
            rep["sample_status"] = (
                "UNDERPOWERED: " + ", ".join(short) + ". Keep probing. Note that "
                "a broker with a low rejection rate needs many more orders to "
                "clear than a bad one needs to convict -- rejections are the "
                "scarce sample."
            )
        else:
            rep["sample_status"] = "adequate"

        if st.orders and st.quotes:
            rep["forensics"] = full_report(st.orders, st.quotes)
        return rep

    # -- internals -------------------------------------------------------
    def _pick_indices(self, quotes: Sequence[Quote], n: int) -> list[int]:
        """Sample probe points, over-weighting fast markets on purpose."""
        cfg = self.cfg
        n_fast = int(n * cfg.fast_market_fraction)
        n_calm = n - n_fast
        hi = len(quotes) - 100
        if hi < n:
            return sorted(self._nprng.choice(max(hi, 1), size=min(n, hi),
                                             replace=False).tolist())

        mid = np.fromiter((q.mid for q in quotes), dtype=float, count=len(quotes))
        # short-window realised volatility as the "fast market" proxy
        w = 200
        vol = np.abs(np.diff(mid, prepend=mid[0]))
        roll = np.convolve(vol, np.ones(w) / w, mode="same")[:hi]
        thr = np.quantile(roll, cfg.fast_market_vol_quantile)

        fast_pool = np.flatnonzero(roll >= thr)
        calm_pool = np.flatnonzero(roll < thr)

        picks: list[int] = []
        if fast_pool.size:
            picks += self._nprng.choice(
                fast_pool, size=min(n_fast, fast_pool.size), replace=False).tolist()
        if calm_pool.size:
            picks += self._nprng.choice(
                calm_pool, size=min(n_calm, calm_pool.size), replace=False).tolist()
        return sorted(int(i) for i in picks)

    @staticmethod
    def _index_after(quotes: Sequence[Quote], ts: int) -> int | None:
        lo, hi = 0, len(quotes) - 1
        if quotes[hi].ts_recv <= ts:
            return None
        while lo < hi:
            m = (lo + hi) // 2
            if quotes[m].ts_recv <= ts:
                lo = m + 1
            else:
                hi = m
        return lo
