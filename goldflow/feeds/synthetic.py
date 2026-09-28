"""
Synthetic gold-futures feed.

Purpose: make the entire pipeline runnable and testable BEFORE any API key
exists, and -- more importantly -- give the estimators a dataset whose true
parameters are known, so a failing estimator can be distinguished from a
failing market.

This is a limit-order-book simulator, not a price-path generator. Mid-price
moves emerge from queue depletion, which means the Cont-Kukanov-Stoikov OFI
relationship is a *consequence* of the mechanics rather than something baked
in by construction. If the OFI regression cannot recover structure here, the
implementation is broken -- that is the whole point of having this file.

Also generates a lagged, basis-offset spot leg so lead-lag and information
share code has something with a known answer (the futures leg leads by
construction, by `lag_ms`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np

from .base import NS_PER_MS, NS_PER_S, Quote, Side, Trade


@dataclass
class SynthConfig:
    n_events: int = 200_000
    start_px: float = 3_450.0
    tick: float = 0.10
    seed: int = 7

    # event mix
    p_trade: float = 0.18          # aggressive orders
    p_add: float = 0.46            # passive adds
    # remainder is cancels

    # queue sizes (contracts)
    queue_mean: float = 90.0
    queue_min: int = 5
    order_size_mean: float = 6.0
    trade_size_mean: float = 9.0

    # arrival process
    mean_interarrival_ms: float = 12.0

    # directional pressure: drifts slowly so the book has regimes
    drift_halflife_events: float = 4_000.0
    drift_scale: float = 0.09

    # depth regime: multiplies queue sizes, mimics session liquidity cycles
    depth_cycle_events: float = 25_000.0
    depth_amplitude: float = 0.55

    # spot leg
    lag_ms: float = 45.0           # spot follows futures by this much
    basis_mean: float = -0.35      # spot = futures + basis (USD/oz)
    basis_ar: float = 0.9995       # basis is persistent
    basis_shock: float = 0.004
    spot_noise: float = 0.012      # idiosyncratic spot quote noise
    spot_spread: float = 0.22      # typical broker XAUUSD spread in USD


class SyntheticBook:
    """Generates L1 quote events and trades for a single instrument."""

    def __init__(self, cfg: SynthConfig | None = None, symbol: str = "GCZ6"):
        self.cfg = cfg or SynthConfig()
        self.symbol = symbol
        self._rng = np.random.default_rng(self.cfg.seed)

    # -- helpers ---------------------------------------------------------
    def _draw_queue(self, depth_mult: float) -> int:
        c = self.cfg
        lam = max(c.queue_mean * depth_mult, 8.0)
        return int(max(c.queue_min, self._rng.poisson(lam)))

    def stream(self) -> Iterator[Quote | Trade]:
        c = self.cfg
        rng = self._rng

        ts = 1_700_000_000 * NS_PER_S
        tick = c.tick

        depth_mult = 1.0
        bid_px = round(c.start_px / tick) * tick
        ask_px = bid_px + tick
        bid_sz = self._draw_queue(depth_mult)
        ask_sz = self._draw_queue(depth_mult)

        # OU-ish directional pressure in (-1, 1)
        phi = float(np.exp(-np.log(2.0) / c.drift_halflife_events))
        drift = 0.0

        for i in range(c.n_events):
            # --- clock -------------------------------------------------
            dt_ms = rng.exponential(c.mean_interarrival_ms)
            ts += int(dt_ms * NS_PER_MS)

            # --- slow-moving state ------------------------------------
            drift = phi * drift + np.sqrt(1 - phi**2) * rng.normal(0, c.drift_scale)
            drift = float(np.clip(drift, -0.9, 0.9))
            depth_mult = 1.0 + c.depth_amplitude * np.sin(
                2 * np.pi * i / c.depth_cycle_events
            )
            depth_mult = max(depth_mult, 0.25)

            # buy pressure in [0,1]
            pbuy = 0.5 + 0.5 * drift

            u = rng.random()
            if u < c.p_trade:
                # ---- aggressive order ---------------------------------
                buy = rng.random() < pbuy
                size = int(max(1, rng.poisson(c.trade_size_mean)))
                if buy:
                    filled = min(size, ask_sz)
                    yield Trade(ts, ts, ask_px, filled, Side.ASK, self.symbol)
                    ask_sz -= filled
                    if ask_sz <= 0:
                        # offer swept: book steps up one tick
                        bid_px, bid_sz = ask_px, self._draw_queue(depth_mult)
                        ask_px = bid_px + tick
                        ask_sz = self._draw_queue(depth_mult)
                else:
                    filled = min(size, bid_sz)
                    yield Trade(ts, ts, bid_px, filled, Side.BID, self.symbol)
                    bid_sz -= filled
                    if bid_sz <= 0:
                        ask_px, ask_sz = bid_px, self._draw_queue(depth_mult)
                        bid_px = ask_px - tick
                        bid_sz = self._draw_queue(depth_mult)

            elif u < c.p_trade + c.p_add:
                # ---- passive add --------------------------------------
                size = int(max(1, rng.poisson(c.order_size_mean)))
                if rng.random() < pbuy:
                    bid_sz += size
                else:
                    ask_sz += size
            else:
                # ---- cancel -------------------------------------------
                size = int(max(1, rng.poisson(c.order_size_mean)))
                # cancels lean against the pressure: liquidity flees the side
                # that is about to be run over
                if rng.random() < pbuy:
                    ask_sz = max(1, ask_sz - size)
                    if ask_sz <= 1 and rng.random() < 0.25:
                        # offer pulled entirely -> book steps up, no trade
                        bid_px, bid_sz = ask_px, self._draw_queue(depth_mult)
                        ask_px = bid_px + tick
                        ask_sz = self._draw_queue(depth_mult)
                else:
                    bid_sz = max(1, bid_sz - size)
                    if bid_sz <= 1 and rng.random() < 0.25:
                        ask_px, ask_sz = bid_px, self._draw_queue(depth_mult)
                        bid_px = ask_px - tick
                        bid_sz = self._draw_queue(depth_mult)

            yield Quote(ts, ts, bid_px, ask_px, bid_sz, ask_sz, self.symbol)


class SyntheticSpot:
    """Derives a lagged, basis-offset spot leg from a futures quote series.

    Consumes futures quotes (already generated) rather than re-simulating, so
    the lead-lag relationship has an exact known value: `lag_ms`.
    """

    def __init__(self, fut_quotes: list[Quote], cfg: SynthConfig | None = None,
                 symbol: str = "XAUUSD"):
        self.cfg = cfg or SynthConfig()
        self.symbol = symbol
        self._fut = fut_quotes

    def stream(self) -> Iterator[Quote]:
        c = self.cfg
        rng = np.random.default_rng(c.seed + 991)
        lag_ns = int(c.lag_ms * NS_PER_MS)
        basis = c.basis_mean
        half = c.spot_spread / 2.0

        for q in self._fut:
            basis = (c.basis_ar * basis
                     + (1 - c.basis_ar) * c.basis_mean
                     + rng.normal(0, c.basis_shock))
            mid = q.mid + basis + rng.normal(0, c.spot_noise)
            ts = q.ts_event + lag_ns
            yield Quote(
                ts_event=ts,
                ts_recv=ts,
                bid_px=round(mid - half, 3),
                ask_px=round(mid + half, 3),
                # spot "sizes" are notional LP quotes, not a real queue.
                # Deliberately noisy and uninformative -- see report Part 02.
                bid_sz=int(max(1, rng.poisson(40))),
                ask_sz=int(max(1, rng.poisson(40))),
                symbol=self.symbol,
            )


def make_pair(cfg: SynthConfig | None = None) -> tuple[list, list]:
    """Convenience: build (futures_events, spot_quotes) for the harness."""
    cfg = cfg or SynthConfig()
    fut_events = list(SyntheticBook(cfg).stream())
    fut_quotes = [e for e in fut_events if isinstance(e, Quote)]
    spot = list(SyntheticSpot(fut_quotes, cfg).stream())
    return fut_events, spot
