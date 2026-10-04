"""
Standalone backtest (read-only): "Spot order-flow signal -> GC futures execution" idea.
Uses our ALREADY-VALIDATED Portfolio signal timing (RSI-14 Reversion, Volume Imbalance Spike,
ADX-Trend - computed from real spot tick data via QuestDB, same as the live system), but
EXECUTES each trade on GCZ6 futures prices instead of spot: entry price, SL (1x ATR), and the
$2-arm/$1-trail trailing exit are all computed using GC's own 1-min OHLC bars from MT5.

Cost model: GC spread $0.31 (live-sampled), GC swap $0.00 (live-confirmed) - vs spot's $0.24
spread and negative swap on the long side. Only ~68 days of GC history exist (contract start
17-Jul-2026), so spot signals before that date are dropped.

Caveat: GC data here is 1-min OHLC bars only (no tick-level GC feed), so exits are checked bar-
by-bar (high/low vs SL) rather than true tick-by-tick like our spot backtests - a coarser, if
reasonable, approximation given we have no GC tick source.

Run: python backtest_gc_futures_execution.py
"""
from datetime import datetime, timezone

import MetaTrader5 as mt5

from backtest_9080_all_strategies_oos import load_ticks
from backtest_adx_type1_vs_type2 import build_bars_with_flow, strategy_adx_vwap_trend
from backtest_iteration_round1 import build_bars_full, calc_atr_series
from backtest_smc_strategies_v2_full import strategy_rsi_reversion, strategy_volume_imbalance

TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
SL_ATR_MULT = 1.0
GC_SPREAD = 0.31
SPOT_SPREAD = 0.24
GC_CONTRACT_START = datetime(2026, 7, 17, tzinfo=timezone.utc).timestamp()


def load_gc_bars():
    mt5.initialize()
    rates = mt5.copy_rates_from_pos("GCZ6", mt5.TIMEFRAME_M1, 0, 200000)
    mt5.shutdown()
    bars = []
    for r in rates:
        bars.append({"t": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["tick_volume"])})
    bars.sort(key=lambda b: b["t"])
    return bars


def calc_atr_gc(bars, period=14):
    atrs = [2.0] * len(bars)
    for i in range(period, len(bars)):
        trs = []
        for j in range(i - period + 1, i + 1):
            h, l, pc = bars[j]["high"], bars[j]["low"], bars[j - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atrs[i] = sum(trs) / len(trs)
    return atrs


def run_bar_trailing_exit(gc_bars, start_idx, d, entry, sl0):
    """Bar-level (not tick-level) trailing exit simulation on GC 1-min bars."""
    long_ = d == "BUY"
    best = entry
    sl = sl0
    armed = False
    for k in range(start_idx, len(gc_bars)):
        b = gc_bars[k]
        if long_:
            best = max(best, b["high"])
            if not armed and (best - entry) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = max(sl, best - TRAIL_DIST)
            if b["low"] <= sl:
                return sl, k, ("TRAIL" if armed else "LOSS")
        else:
            best = min(best, b["low"])
            if not armed and (entry - best) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = min(sl, best + TRAIL_DIST)
            if b["high"] >= sl:
                return sl, k, ("TRAIL" if armed else "LOSS")
    return gc_bars[-1]["close"], len(gc_bars) - 1, "OPEN"


def main():
    print("Loading spot ticks (for signal generation, same as live)...")
    ts, px, vol, buy = load_ticks()
    spot_bars = build_bars_full(ts, px, vol, 60)
    spot_bars_flow = build_bars_with_flow(ts, px, vol, buy)
    atrs_spot = calc_atr_series(spot_bars)

    rsi_sigs = strategy_rsi_reversion(spot_bars)
    vi_sigs = strategy_volume_imbalance(spot_bars)
    adxtrend_sigs = strategy_adx_vwap_trend(spot_bars_flow)
    all_sigs = {"RSI14": rsi_sigs, "VOLIMB": vi_sigs, "ADXTREND": adxtrend_sigs}
    print(f"Signals (spot-derived): RSI14={len(rsi_sigs)} VOLIMB={len(vi_sigs)} ADXTREND={len(adxtrend_sigs)}\n")

    print("Loading GC futures bars from MT5...")
    gc_bars = load_gc_bars()
    gc_by_t = {b["t"]: i for i, b in enumerate(gc_bars)}
    atrs_gc = calc_atr_gc(gc_bars)
    print(f"GC bars: {len(gc_bars)} ({datetime.fromtimestamp(gc_bars[0]['t'],timezone.utc).date()} to "
          f"{datetime.fromtimestamp(gc_bars[-1]['t'],timezone.utc).date()})\n")

    for name, sigs in all_sigs.items():
        results_spot_exec, results_gc_exec = [], []
        for i, d in sigs:
            sig_t = spot_bars[i]["t"] if name != "ADXTREND" else spot_bars_flow[i]["t"]
            if sig_t < GC_CONTRACT_START:
                continue
            entry_t = sig_t + 60  # next bar
            gi = gc_by_t.get(entry_t)
            if gi is None or gi + 1 >= len(gc_bars):
                continue
            entry = gc_bars[gi]["open"]
            atr = atrs_gc[gi]
            if atr <= 0:
                continue
            sl = entry - SL_ATR_MULT * atr if d == "BUY" else entry + SL_ATR_MULT * atr
            xp, xi, why = run_bar_trailing_exit(gc_bars, gi, d, entry, sl)
            if why == "OPEN":
                continue
            gross = (xp - entry) if d == "BUY" else (entry - xp)
            results_gc_exec.append(gross - GC_SPREAD)

        n = len(results_gc_exec)
        if n == 0:
            print(f"{name}: no GC-executable trades (signals before contract start or unmatched bars)")
            continue
        net = sum(results_gc_exec)
        eq, peak, dd = 100.0, 100.0, 0.0
        for pnl in results_gc_exec:
            eq += pnl
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        wins = sum(1 for p in results_gc_exec if p > 0)
        print(f"{name} (GC execution): trades {n:4d} | WR {wins/n*100:5.1f}% | NET ${net:8.2f} | "
              f"net/trade ${net/n:6.3f} | MaxDD {dd:5.1f}%")


if __name__ == "__main__":
    main()
