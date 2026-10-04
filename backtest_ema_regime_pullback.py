"""
Standalone backtest (read-only): SSRN-inspired "Regime-Filtered EMA Pullback" strategy for gold
(Bhatti, "A Regime-Filtered Intraday Trading Framework for Gold: VWAP Microstructure + EMA-Based
Dynamic Exit Mechanisms"). Paper used 15-min XAU/USD, 200-EMA regime filter, 50-EMA pullback +
rejection entries. Exact numeric thresholds/exit weren't disclosed in the abstract, so this is our
own reasonable, clearly-labeled interpretation - tested with OUR real tick data and OUR cost model
(not the paper's own claimed results, which we cannot verify).

Rule (this implementation):
  - 15-min bars (closer to paper's timeframe than our usual 1-min).
  - Regime: A (up) if close > EMA200, B (down) if close < EMA200 (both computed causally on 15-min closes).
  - Pullback + rejection:
      Regime A: bar's low touches/dips into EMA50 (low <= EMA50) AND bar closes back above EMA50
                AND bar is green (close > open)  -> BUY
      Regime B: mirror (high >= EMA50, closes back below, red candle) -> SELL
  - Entry at the open of the next 15-min bar.
  - Two exit variants tested side by side:
      (a) ATR-based: SL = entry -/+ 1.0*ATR(14, 15-min), TP = entry +/- 2.0*ATR (our established RR)
      (b) Dynamic EMA-exit (matches the paper's own description): exit when price closes back
          across EMA50 against the trade, else a wide ATR-based catastrophe stop as a backstop.
  - Spread $0.24/trade, tick-level exit checking, market-hours only, FULL period.
  - One trade per regime+pullback event (not one per stretch - a fresh pullback can re-signal).

Run: python backtest_ema_regime_pullback.py
"""
from datetime import datetime, timezone
import numpy as np

from backtest_9080_all_strategies_oos import load_ticks, run_exit, pnl_of
from backtest_vwap_first_close import market_closed

SPREAD = 0.24
BAR_SECONDS = 15 * 60
EMA_FAST = 50
EMA_SLOW = 200
ATR_PERIOD = 14
SL_MULT = 1.0
TP_MULT = 2.0


def build_bars_15m(ts, px, vol):
    bars = []
    cur = None
    for i in range(len(ts)):
        bucket = int(ts[i] // BAR_SECONDS) * BAR_SECONDS
        p, v = px[i], vol[i]
        if cur is None or cur["t"] < bucket:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": bucket, "open": p, "high": p, "low": p, "close": p, "volume": v}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
    return bars


def compute_indicators(bars):
    closes = [b["close"] for b in bars]
    ema_fast = ema_slow = None
    kf, ks = 2 / (EMA_FAST + 1), 2 / (EMA_SLOW + 1)
    for i, c in enumerate(closes):
        ema_fast = c if ema_fast is None else c * kf + ema_fast * (1 - kf)
        ema_slow = c if ema_slow is None else c * ks + ema_slow * (1 - ks)
        bars[i]["ema_fast"] = ema_fast
        bars[i]["ema_slow"] = ema_slow
    for i in range(len(bars)):
        if i < ATR_PERIOD:
            bars[i]["atr"] = 2.0
            continue
        trs = []
        for j in range(i - ATR_PERIOD + 1, i + 1):
            h, l, pc = bars[j]["high"], bars[j]["low"], bars[j - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        bars[i]["atr"] = sum(trs) / len(trs)


def make_signals(bars, warmup=210):
    sigs = []
    for i in range(warmup, len(bars) - 1):
        b = bars[i]
        ema50, ema200 = b["ema_fast"], b["ema_slow"]
        green, red = b["close"] > b["open"], b["close"] < b["open"]
        if b["close"] > ema200 and b["low"] <= ema50 and b["close"] > ema50 and green:
            sigs.append((i, "BUY"))
        elif b["close"] < ema200 and b["high"] >= ema50 and b["close"] < ema50 and red:
            sigs.append((i, "SELL"))
    return sigs


def run_exit_ema(ts, px, start, d, entry, sl_catastrophe, ema_getter):
    """Dynamic exit: close back across EMA50 against the trade, else catastrophe ATR stop."""
    long_ = d == "BUY"
    for k in range(start, len(ts)):
        p = px[k]
        ema50 = ema_getter(ts[k])
        if long_:
            if p <= sl_catastrophe:
                return sl_catastrophe, k, "CAT_SL"
            if ema50 is not None and p < ema50:
                return p, k, "EMA_EXIT"
        else:
            if p >= sl_catastrophe:
                return sl_catastrophe, k, "CAT_SL"
            if ema50 is not None and p > ema50:
                return p, k, "EMA_EXIT"
    return px[-1], len(ts) - 1, "OPEN"


def execute_atr(bars, ts, px, sigs):
    trades, free_idx = [], -1
    for i, d in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        atr = b["atr"]
        if d == "BUY":
            sl = entry - SL_MULT * atr
            tp = entry + TP_MULT * atr
        else:
            sl = entry + SL_MULT * atr
            tp = entry - TP_MULT * atr
        xp, xi, why = run_exit(ts, px, nxt, d, sl, tp)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def execute_ema_exit(bars, ts, px, sigs):
    # Build a bar-index-by-time lookup so we can fetch "the EMA50 as of the bar containing time t"
    bar_starts = [b["t"] for b in bars]

    def ema_at(t):
        import bisect
        idx = bisect.bisect_right(bar_starts, t) - 1
        idx = min(idx, len(bars) - 1)
        return bars[idx]["ema_fast"] if idx >= 0 else None

    trades, free_idx = [], -1
    for i, d in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        atr = b["atr"]
        cat_sl = entry - 3.0 * atr if d == "BUY" else entry + 3.0 * atr
        xp, xi, why = run_exit_ema(ts, px, nxt, d, entry, cat_sl, ema_at)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    wins = sum(1 for x in tr if x["pnl"] > 0)
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, wr=wins / n * 100, gross=gross, net=net, dd=dd, avg=net / n)


def row(label, s):
    if s is None:
        return f"   {label:28s}: no trades"
    return (f"   {label:28s}: trades {s['n']:4d} | WR {s['wr']:5.1f}% | gross ${s['gross']:8.2f} | "
            f"NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days of tick data, FULL period, spread ${SPREAD}\n")
    bars = build_bars_15m(ts, px, vol)
    print(f"{len(bars)} 15-min bars reconstructed (paper used 15-min XAU/USD)")
    compute_indicators(bars)
    sigs = make_signals(bars)
    print(f"{len(sigs)} regime+pullback+rejection signals found (200-EMA regime, 50-EMA pullback)\n")

    print("=" * 100)
    print("VARIANT A: ATR-based SL/TP (1x ATR SL, 2x ATR TP) - our established RR framework")
    print("=" * 100)
    print(row("ATR exit", stats(execute_atr(bars, ts, px, sigs))))
    print(row("  BUY only", stats(execute_atr(bars, ts, px, [s for s in sigs if s[1] == "BUY"]))))
    print(row("  SELL only", stats(execute_atr(bars, ts, px, [s for s in sigs if s[1] == "SELL"]))))

    print("\n" + "=" * 100)
    print("VARIANT B: Dynamic EMA50-cross exit (matches paper's 'EMA-based dynamic exit' description)")
    print("=" * 100)
    tr_b = execute_ema_exit(bars, ts, px, sigs)
    print(row("EMA-exit", stats(tr_b)))
    for reason in ("EMA_EXIT", "CAT_SL"):
        sub = [x for x in tr_b if x["why"] == reason]
        print(row(f"  exit={reason}", stats(sub)))

    print(f"\nEMA200 warmup note: needs {EMA_SLOW} 15-min bars (~{EMA_SLOW*15/60:.0f} hours of data) to stabilize;")
    print(f"our {len(bars)}-bar history gives limited effective sample after warmup - treat as first-pass only.")


if __name__ == "__main__":
    main()
