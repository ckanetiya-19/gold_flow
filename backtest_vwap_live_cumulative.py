"""
Standalone backtest (read-only): FINAL 6 variants, re-tested with the LIVE-CHART VWAP
(cumulative since the start of available data, NEVER reset daily) - matching what the
dashboards actually compute now (cum_pv/cum_vol globals that only reset on process restart),
instead of the earlier daily-reset VWAP the original backtests used.

Everything else identical to the live logic: entry rules, SL (candle or VWAP-buffer $0.50),
TP (2xSL + 1:1 BE) or Trailing ($2 arm / $1 trail), one position at a time, spread $0.24,
market-hours only, FULL period only (no in-sample/out-of-sample split, per latest request).

Run: python backtest_vwap_live_cumulative.py
"""
from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_vwap_retest import make_signals, stats as rt_stats, row as rt_row
from backtest_vwap_first_close_be import execute as tp_exec
from backtest_vwap_trailing import execute_trailing


def build_bars_cumulative(ts, px, vol):
    """Same as build_bars() but VWAP is cumulative from the FIRST tick in the dataset,
    never reset by day - matching update_mark_price/process_tick's cum_pv/cum_vol globals,
    which only reset when the port process restarts."""
    bars = []
    cur = None
    cum_pv = cum_vol = 0.0
    for i in range(len(ts)):
        minute = int(ts[i] // 60) * 60
        p, v = px[i], vol[i]
        cum_pv += p * v
        cum_vol += v
        vwap = cum_pv / cum_vol if cum_vol > 0 else p
        if cur is None or cur["t"] < minute:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": minute, "open": p, "high": p, "low": p, "close": p, "vwap": vwap}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["vwap"] = vwap
    return bars


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars_cumulative(ts, px, vol)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, {len(bars)} 1-min bars, FULL period, live-chart (cumulative, no daily reset) VWAP\n")
    print(f"VWAP path: starts ${bars[0]['vwap']:.2f}, ends ${bars[-1]['vwap']:.2f}  "
          f"(price: starts ${bars[0]['close']:.2f}, ends ${bars[-1]['close']:.2f})\n")

    first = make_signals(bars, 0.0, True, False)
    retest_b = make_signals(bars, 0.5, False, True)

    variants = [
        ("V1", "First-close, Candle-SL, Fixed 2xSL TP +BE", lambda: tp_exec(bars, ts, px, [(i, d) for i, d, k in first], use_be=True)),
        ("V4", "First-close, VWAP-SL $0.50, Fixed 2xSL TP +BE", lambda: __import__("backtest_vwap_first_vwapsl").execute(bars, ts, px, first, 0.5)),
        ("T1", "First-close, Candle-SL, Trailing $2/$1", lambda: execute_trailing(bars, ts, px, first)),
        ("T2", "First-close, VWAP-SL $0.50, Trailing $2/$1", lambda: execute_trailing(bars, ts, px, first, 0.5)),
        ("V10", "Retest-B, VWAP-SL $0.50, Fixed 2xSL TP +BE", lambda: __import__("backtest_vwap_retest_vwapsl").execute(bars, ts, px, retest_b, 0.5)),
        ("T3", "Retest-B, VWAP-SL $0.50, Trailing $2/$1", lambda: execute_trailing(bars, ts, px, retest_b, 0.5)),
    ]

    print("=" * 130)
    print(f"{'Var':4s} {'Description':40s} {'Trades':>7s} {'Win/BE':>7s} {'Loss':>5s} {'Gross$':>9s} {'NET$':>9s} {'Net/tr':>7s} {'MaxDD':>6s}")
    print("=" * 130)
    for name, desc, fn in variants:
        tr = fn()
        n = len(tr)
        if n == 0:
            print(f"{name:4s} {desc:40s} {'0':>7s}   -      -        -        -       -      -")
            continue
        wins = sum(1 for x in tr if x.get("why") in ("WIN", "TRAIL"))
        losses = sum(1 for x in tr if x.get("why") == "LOSS")
        gross = sum(x["pnl"] for x in tr)
        net = gross - 0.24 * n
        eq, peak, dd = 100.0, 100.0, 0.0
        for x in sorted(tr, key=lambda z: z["t"]):
            eq += x["pnl"] - 0.24
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
        print(f"{name:4s} {desc:40s} {n:7d} {wins:7d} {losses:5d} {gross:9.2f} {net:9.2f} {net/n:7.3f} {dd:6.1f}")


if __name__ == "__main__":
    main()
