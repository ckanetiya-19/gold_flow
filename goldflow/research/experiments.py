"""
The validation plan, as runnable code.

Ordered so the cheapest experiment that could kill the project runs first.
Stop at the first failure rather than proceeding to build -- that is the whole
point of the ordering, and skipping ahead is how people spend a year building
on a premise that a two-hour test would have refuted.

  1. lead_lag       Does GC actually lead your XAUUSD, by more than your latency?
  2. ofi_regression Does the Cont-Kukanov-Stoikov result reproduce on gold?
  3. tick_volume    How bad is broker tick volume as a proxy for real volume?
  4. signal_returns Does the signal survive YOUR spread and commission?
  5. walk_forward   Does it hold out of sample, across regimes?

Each returns a dict with an explicit `passed` boolean and the number that
decided it. `passed=False` is a result, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from ..core.classify import classification_agreement, classify_stream
from ..core.ofi import OFIBucketer, fit_ofi
from ..feeds.base import NS_PER_MS, NS_PER_S, Quote, Trade
from .backtest import (
    BacktestConfig,
    Backtester,
    flat_strategy,
    ofi_threshold_strategy,
)
from .costs import CostConfig, SpreadModel
from .leadlag import basis_stats, cross_correlation, price_discovery, to_grid


# ---------------------------------------------------------------------------
# Experiment 1
# ---------------------------------------------------------------------------

def experiment_lead_lag(fut: Sequence[Quote], spot: Sequence[Quote],
                        your_latency_ms: float = 50.0,
                        grid_ms: float = 10.0,
                        run_vecm: bool = True) -> dict:
    """Does the futures leg lead, and is the lead longer than your latency?

    Kill criterion: the measured lead is shorter than your round-trip latency,
    OR the futures information share is not clearly above 50%. Either means
    you cannot capture the lead -- someone faster already has.
    """
    xc = cross_correlation(fut, spot, grid_ns=int(grid_ms * NS_PER_MS),
                           max_lag_steps=30)

    out: dict = {
        "experiment": "1_lead_lag",
        "grid_ms": grid_ms,
        "peak_lag_ms": xc.best_lag_ms,
        "peak_corr": round(xc.best_corr, 4),
        "contemporaneous_corr": round(xc.contemporaneous_corr, 4),
        "your_latency_ms": your_latency_ms,
        "lead_exceeds_latency": bool(xc.best_lag_ms > your_latency_ms),
    }

    if run_vecm:
        # Sample at or below the measured lead. A grid coarser than the lead
        # leaves the Cholesky ordering unidentified and ILS can name the wrong
        # venue -- see the table in price_discovery's docstring. Sweeping and
        # selecting on band width is the defensible way to pick the grid; the
        # sweep is reported so the choice is visible rather than assumed.
        lead_ms = abs(xc.best_lag_ms) or grid_ms
        candidates = sorted({max(grid_ms, round(lead_ms / 4)),
                             max(grid_ms, round(lead_ms / 2)),
                             max(grid_ms, round(lead_ms)),
                             max(grid_ms, round(lead_ms * 2.5))})
        sweep, best = [], None
        for g in candidates:
            try:
                r = price_discovery(fut, spot, grid_ns=int(g * NS_PER_MS))
            except Exception as exc:                    # noqa: BLE001
                sweep.append({"grid_ms": g, "error": str(exc)[:90]})
                continue
            band = r.is_upper[0] - r.is_lower[0]
            sweep.append({
                "grid_ms": g,
                "is_futures": round(r.is_mid[0], 3),
                "is_band_width": round(band, 3),
                "cs_futures": round(r.component_share[0], 3),
                "ils_futures": round(r.ils[0], 3),
                "n_obs": r.n_obs,
                "identified": bool(band <= 0.35),
            })
            if band <= 0.35 and r.n_obs >= 2_000 and best is None:
                best = r

        out["vecm_grid_sweep"] = sweep
        if best is not None:
            out.update({
                "selected_grid_ms": best.n_obs and next(
                    (s["grid_ms"] for s in sweep
                     if s.get("n_obs") == best.n_obs), None),
                "hasbrouck_is_futures": round(best.is_mid[0], 4),
                "hasbrouck_is_band": (round(best.is_lower[0], 4),
                                      round(best.is_upper[0], 4)),
                "component_share_futures": round(best.component_share[0], 4),
                "ils_futures": round(best.ils[0], 4),
                "vecm_n_obs": best.n_obs,
            })
            futures_leads = best.ils[0] > 0.5
        else:
            out["vecm_note"] = ("No grid gave an identified decomposition. "
                                "Falling back to the cross-correlation sign.")
            futures_leads = xc.best_lag_ms > 0
    else:
        futures_leads = xc.best_lag_ms > 0

    out["passed"] = bool(futures_leads and out["lead_exceeds_latency"])
    out["note"] = (
        "Futures lead and the lead is longer than your latency -- proceed."
        if out["passed"] else
        "Either futures do not lead here, or the lead is inside your latency. "
        "Check clock alignment on both legs before concluding anything."
    )
    try:
        out["basis"] = str(basis_stats(fut, spot))
    except Exception:                                   # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------
# Experiment 2
# ---------------------------------------------------------------------------

def experiment_ofi_regression(fut: Sequence[Quote],
                              intervals_ns: Sequence[int] = (
                                  100 * NS_PER_MS, NS_PER_S, 10 * NS_PER_S),
                              min_r2: float = 0.05) -> dict:
    """Reproduce dP = beta * OFI on gold futures, at several horizons.

    Reference: average R^2 ~= 65% at 10-second intervals in equities, with
    lambda ~= 0.98 in beta = c / AD^lambda.

    IMPORTANT -- two different questions get answered here, and conflating them
    is the most consequential error available in this whole project:

      contemporaneous R^2  does OFI EXPLAIN the move in the same interval?
                           This is what the paper reports. It is close to a
                           definition of what a price move is.
      predictive R^2       does OFI at k say anything about the move at k+1?
                           This is the only one that can be traded.

    Expect the second to be a small fraction of the first. `passed` is keyed to
    the CONTEMPORANEOUS number, because that is what validates the plumbing --
    if it is near zero your quote data is not real book updates. Whether there
    is a TRADE is decided by `predictive_r2` here and by Experiment 4.
    """
    rows = []
    for iv in intervals_ns:
        buckets = list(OFIBucketer(iv).run(fut))
        if len(buckets) < 50:
            rows.append({"interval_ms": iv / NS_PER_MS, "error": "too few buckets"})
            continue
        fit = fit_ofi(buckets)
        rows.append({
            "interval_ms": iv / NS_PER_MS,
            "n_buckets": fit.n,
            "beta": fit.beta,
            "contemporaneous_r2": round(fit.r2, 4),
            "t_stat": round(fit.t_stat, 1),
            "predictive_r2": round(fit.r2_predictive, 5),
            "predictive_t": round(fit.t_predictive, 1),
            "r2_depth_normalised": round(fit.r2_normalised, 4),
            "lambda_hat": None if fit.lambda_hat is None else round(fit.lambda_hat, 3),
            "r2_of_depth_fit": None if fit.r2_depth is None else round(fit.r2_depth, 3),
        })

    best = max((r.get("contemporaneous_r2", 0.0) for r in rows), default=0.0)
    best_pred = max((r.get("predictive_r2", 0.0) for r in rows), default=0.0)
    best_pred_t = max((abs(r.get("predictive_t", 0.0)) for r in rows), default=0.0)
    lam = next((r.get("lambda_hat") for r in rows
                if r.get("lambda_hat") is not None), None)

    if best < min_r2:
        note = ("No usable OFI relationship at these horizons. Check that "
                "quotes are real L1 book updates and not sampled/throttled "
                "snapshots -- throttled feeds destroy e_n.")
    elif best_pred_t < 3.0:
        note = ("OFI EXPLAINS gold's moves but does not forecast them at these "
                "horizons (predictive t < 3). The impact result reproduces; "
                "the trade does not follow from it. Do not add features until "
                "something fits -- that is how overfitting starts.")
    else:
        note = ("OFI both explains and carries some forward information. "
                "Proceed to Experiment 4 and subtract your real costs.")

    return {
        "experiment": "2_ofi_regression",
        "by_interval": rows,
        "best_contemporaneous_r2": round(best, 4),
        "best_predictive_r2": round(best_pred, 5),
        "best_predictive_t": round(best_pred_t, 1),
        "lambda_hat": lam,
        "lambda_near_one": (lam is not None and 0.6 <= lam <= 1.4),
        "passed": bool(best >= min_r2),
        "tradeable_signal_present": bool(best_pred_t >= 3.0),
        "note": note,
    }


# ---------------------------------------------------------------------------
# Experiment 3
# ---------------------------------------------------------------------------

def experiment_tick_volume_proxy(fut_trades: Sequence[Trade],
                                 spot_quotes: Sequence[Quote],
                                 bar_ns: int = 60 * NS_PER_S) -> dict:
    """Correlate broker tick COUNT against real futures VOLUME, bar by bar.

    This quantifies exactly how much information the MT5-style tick volume
    carries. The expected answer is "some, about activity; none about size or
    direction". Running it is worth the hour either way: if the correlation is
    high on your broker, a degraded fallback becomes defensible; if it is low,
    you have the number to point at next time someone sells you a footprint
    indicator for a spot symbol.
    """
    if not fut_trades or not spot_quotes:
        return {"experiment": "3_tick_volume_proxy", "error": "empty inputs"}

    # Shrink the bar if the sample is too short to give a usable number of
    # them. A correlation over 25 bars is not a measurement.
    span_ns = max(fut_trades[-1].ts_recv - fut_trades[0].ts_recv, 1)
    if span_ns / bar_ns < 100:
        bar_ns = max(int(span_ns // 200), NS_PER_S)

    def bar_key(ts: int) -> int:
        return ts // bar_ns

    real: dict[int, int] = {}
    for t in fut_trades:
        real[bar_key(t.ts_recv)] = real.get(bar_key(t.ts_recv), 0) + t.size

    ticks: dict[int, int] = {}
    last: tuple[float, float] | None = None
    for q in spot_quotes:
        cur = (q.bid_px, q.ask_px)
        if cur != last:                     # a "tick" is a QUOTE CHANGE
            k = bar_key(q.ts_recv)
            ticks[k] = ticks.get(k, 0) + 1
            last = cur

    keys = sorted(set(real) & set(ticks))
    if len(keys) < 30:
        return {"experiment": "3_tick_volume_proxy",
                "error": f"only {len(keys)} overlapping bars"}

    v = np.array([real[k] for k in keys], float)
    c = np.array([ticks[k] for k in keys], float)

    pearson = float(np.corrcoef(v, c)[0, 1])
    spearman = float(pd.Series(v).corr(pd.Series(c), method="spearman"))
    log_r = float(np.corrcoef(np.log1p(v), np.log1p(c))[0, 1])

    return {
        "experiment": "3_tick_volume_proxy",
        "bars": len(keys),
        "bar_seconds": bar_ns / NS_PER_S,
        "pearson": round(pearson, 4),
        "spearman": round(spearman, 4),
        "log_log_pearson": round(log_r, 4),
        "passed": True,          # informational; there is no failure mode
        "note": (
            "Tick count tracks ACTIVITY. It carries no size and no aggressor "
            "side, so it cannot produce delta, CVD or a footprint no matter "
            "how high this correlation is."
        ),
    }


def experiment_classification_accuracy(events: Sequence[Quote | Trade]) -> dict:
    """How well do the fallback classifiers recover the exchange aggressor flag?

    Run once on a sample that HAS the flag. The resulting accuracy is the error
    bar on every delta-derived quantity you build without the flag.
    """
    res = classification_agreement(events)
    return {
        "experiment": "3b_classification_accuracy",
        **res,
        "passed": bool(res.get("lee_ready") or 0 > 0.8),
        "note": "Use the exchange flag wherever it exists; these are fallbacks.",
    }


# ---------------------------------------------------------------------------
# Experiment 4
# ---------------------------------------------------------------------------

def experiment_signal_returns(fut: Sequence[Quote], spot: Sequence[Quote],
                              horizons_s: Sequence[float] = (5, 30, 300),
                              bucket_ns: int = NS_PER_S,
                              z_threshold: float = 2.0,
                              costs: CostConfig | None = None) -> dict:
    """Expected move after a signal, at several horizons, NET of your costs.

    This is where most candidates die, and it should be run before any
    backtest: if the average favourable move is smaller than the round-trip
    cost, no position-sizing or stop placement rescues it.
    """
    from ..core.ofi import RollingOFIZScore

    spreads = SpreadModel(costs or CostConfig())
    grid = to_grid(spot, int(0.5 * NS_PER_S))
    if grid.empty:
        return {"experiment": "4_signal_returns", "error": "no spot grid"}

    g_ts = grid.index.to_numpy()
    g_px = grid.to_numpy()

    z = RollingOFIZScore(600, 60)
    sig_ts: list[int] = []
    sig_dir: list[int] = []
    for b in OFIBucketer(bucket_ns).run(fut):
        zz = z.update(b)
        if zz is None or abs(zz) < z_threshold:
            continue
        sig_ts.append(b.ts_end)
        sig_dir.append(1 if zz > 0 else -1)

    if len(sig_ts) < 20:
        return {"experiment": "4_signal_returns",
                "error": f"only {len(sig_ts)} signals at z>{z_threshold}"}

    ts = np.asarray(sig_ts)
    d = np.asarray(sig_dir)
    i0 = np.searchsorted(g_ts, ts, side="right") - 1
    ok = i0 >= 0

    rows = []
    for h in horizons_s:
        i1 = np.searchsorted(g_ts, ts + int(h * NS_PER_S), side="right") - 1
        m = ok & (i1 < g_px.size) & (i1 > i0)
        if m.sum() < 20:
            rows.append({"horizon_s": h, "error": "too few paired observations"})
            continue
        move = (g_px[i1[m]] - g_px[i0[m]]) * d[m]
        rt = np.array([spreads.round_trip(int(t), int(t)) for t in ts[m]])
        net = move - rt
        rows.append({
            "horizon_s": h,
            "n": int(m.sum()),
            "mean_gross_move": round(float(move.mean()), 4),
            "mean_round_trip_cost": round(float(rt.mean()), 4),
            "mean_net": round(float(net.mean()), 4),
            "hit_rate": round(float((move > 0).mean()), 4),
            "net_positive": bool(net.mean() > 0),
            "t_stat": round(float(net.mean() / (net.std(ddof=1) / np.sqrt(net.size)))
                            if net.std(ddof=1) > 0 else 0.0, 2),
        })

    any_net = any(r.get("net_positive") for r in rows)
    return {
        "experiment": "4_signal_returns",
        "z_threshold": z_threshold,
        "n_signals": len(sig_ts),
        "by_horizon": rows,
        "passed": bool(any_net),
        "note": ("At least one horizon survives costs."
                 if any_net else
                 "Gross move is smaller than the round trip at every horizon. "
                 "Stop here; parameter tuning from this point is overfitting."),
    }


# ---------------------------------------------------------------------------
# Experiment 5
# ---------------------------------------------------------------------------

def experiment_walk_forward(fut: Sequence[Quote], spot: Sequence[Quote],
                            n_folds: int = 4,
                            cfg: BacktestConfig | None = None,
                            entry_z: float = 2.0) -> dict:
    """Sequential out-of-sample folds. No refitting -- the signal has no
    fitted parameters, which is deliberate: it removes the largest source of
    overfitting before it can start.

    Kill criterion: results concentrated in one fold. A strategy that makes
    all its money in a single regime has found a regime, not an edge.
    """
    cfg = cfg or BacktestConfig()
    bt = Backtester(cfg)
    strat = ofi_threshold_strategy(entry_z=entry_z)

    n = len(fut)
    m = len(spot)
    if n < 5_000 or m < 5_000:
        return {"experiment": "5_walk_forward", "error": "not enough data"}

    fold_res = []
    for k in range(n_folds):
        f = fut[k * n // n_folds:(k + 1) * n // n_folds]
        lo, hi = f[0].ts_recv, f[-1].ts_recv
        s = [q for q in spot if lo <= q.ts_recv <= hi + 10 * NS_PER_S]
        if len(f) < 1_000 or len(s) < 500:
            continue
        r = bt.run(f, s, strat)
        fold_res.append({"fold": k, **r.summary()})

    if not fold_res:
        return {"experiment": "5_walk_forward", "error": "no usable folds"}

    nets = np.array([f["net_pnl"] for f in fold_res])
    pos = int((nets > 0).sum())
    conc = float(np.max(np.abs(nets)) / (np.sum(np.abs(nets)) or 1.0))

    # a control run that never trades; its net must be exactly zero
    control = bt.run(fut[:n // n_folds], spot, flat_strategy())

    return {
        "experiment": "5_walk_forward",
        "folds": fold_res,
        "total_net": round(float(nets.sum()), 2),
        "positive_folds": f"{pos}/{len(fold_res)}",
        "concentration": round(conc, 3),
        "control_net_pnl": round(control.net_pnl, 6),
        "accounting_ok": abs(control.net_pnl) < 1e-9,
        "passed": bool(nets.sum() > 0 and pos > len(fold_res) / 2 and conc < 0.8),
        "note": ("Positive across the majority of folds without single-fold "
                 "concentration." if nets.sum() > 0 and conc < 0.8 else
                 "Concentrated or negative. Treat as no edge."),
        "read_with_experiment_4": (
            "If Experiment 4 showed a positive net move at a horizon but this "
            "fails, the EXIT RULE is throwing the edge away, not the signal. "
            "ofi_threshold_strategy exits when |z| falls below exit_z, which "
            "is typically seconds -- far shorter than the horizon where the "
            "move accumulates. Fix the holding period before touching the "
            "entry logic."
        ),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all(fut_events, spot_quotes, your_latency_ms: float = 50.0,
            verbose: bool = True) -> dict:
    """Run the plan in order and stop at the first hard failure."""
    fut_quotes = [e for e in fut_events if isinstance(e, Quote)]
    fut_trades = [t for t in classify_stream(fut_events)]

    results: dict[str, dict] = {}
    order = [
        ("1_lead_lag", lambda: experiment_lead_lag(
            fut_quotes, spot_quotes, your_latency_ms)),
        ("2_ofi_regression", lambda: experiment_ofi_regression(fut_quotes)),
        ("3_tick_volume_proxy", lambda: experiment_tick_volume_proxy(
            fut_trades, spot_quotes)),
        ("4_signal_returns", lambda: experiment_signal_returns(
            fut_quotes, spot_quotes)),
        ("5_walk_forward", lambda: experiment_walk_forward(
            fut_quotes, spot_quotes)),
    ]

    for name, fn in order:
        try:
            r = fn()
        except Exception as exc:                        # noqa: BLE001
            r = {"experiment": name, "error": repr(exc), "passed": False}
        results[name] = r
        if verbose:
            _print_result(r)
        if name in ("1_lead_lag", "2_ofi_regression") and not r.get("passed"):
            results["stopped_at"] = name
            if verbose:
                print(f"\n  STOPPING: {name} failed. "
                      "The premise does not hold; do not build on it.\n")
            break
    return results


def _print_result(r: dict) -> None:
    name = r.get("experiment", "?")
    status = "PASS" if r.get("passed") else ("ERROR" if r.get("error") else "FAIL")
    print(f"\n[{status}] {name}")
    for k, v in r.items():
        if k in ("experiment", "passed", "note"):
            continue
        if isinstance(v, list):
            for row in v:
                print(f"    - {row}")
        else:
            print(f"    {k}: {v}")
    if r.get("note"):
        print(f"    -> {r['note']}")
