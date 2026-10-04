"""
Standalone backtest (read-only): FIRST-CLOSE entries with SL anchored to the VWAP line.
  BUY : SL = VWAP - $0.50 ; SELL : SL = VWAP + $0.50   (VWAP of the signal candle)
  TP = 2 x risk, 1:1 break-even, one trade per stretch, one position at a time, entry = first tick of
  next candle, tick-level exits, spread $0.24 per trade, market hours only, FULL period.
Compared with the original candle-SL version (SL = candle low -$1 / high +$1).
Run: python backtest_vwap_first_vwapsl.py
"""
from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars, market_closed
from backtest_vwap_first_close_be import run_exit_be
from backtest_vwap_retest import make_signals, stats, row, SL_BUFFER, TP_MULT


def execute(bars, ts, px, sigs, vwap_buf=None):
    trades, free_idx = [], -1
    for i, d, kind in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        if d == "BUY":
            sl = (b["vwap"] - vwap_buf) if vwap_buf is not None else (b["low"] - SL_BUFFER)
            if entry <= sl:
                continue
            risk = entry - sl
            tp = entry + TP_MULT * risk
        else:
            sl = (b["vwap"] + vwap_buf) if vwap_buf is not None else (b["high"] + SL_BUFFER)
            if entry >= sl:
                continue
            risk = sl - entry
            tp = entry - TP_MULT * risk
        xp, xi, why = run_exit_be(ts, px, nxt, d, entry, sl, tp, risk, True)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "te": ts[xi], "d": d, "kind": kind, "risk": risk, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, FULL period only, spread $0.24, TP=2xSL, 1:1 BE\n")
    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        sigs = make_signals(bars, 0.0, True, False)
        print("=" * 170)
        print(f"{name}: {len(bars)} bars | {len(sigs)} first-close signals")
        print("=" * 170)
        for label, buf in (("ORIGINAL candle-SL (low/high -+ $1)", None), ("VWAP-SL buffer $0.50", 0.5)):
            tr = execute(bars, ts, px, sigs, buf)
            print(row(label, stats(tr)))
            print(row("   BUY only", stats([x for x in tr if x["d"] == "BUY"])))
            print(row("   SELL only", stats([x for x in tr if x["d"] == "SELL"])))
        print()


if __name__ == "__main__":
    main()
