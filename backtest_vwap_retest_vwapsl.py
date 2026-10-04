"""
Standalone backtest (read-only): VWAP retest entries with SL anchored to the VWAP LINE.
  First-close entries: unchanged (SL = candle low -$1 / high +$1).
  RETEST entries     : BUY  SL = VWAP - buffer ; SELL SL = VWAP + buffer   (VWAP of the signal candle)
  TP = 2 x risk, 1:1 break-even, one position at a time, entry = first tick of next candle,
  tick-level exits, spread $0.24 per trade, market hours only, FULL period.
Buffers tested: $0.50, $1.00, $1.50.  Retest A = strict touch, B = within $0.50 of VWAP.
Compared with: BASELINE (first-close only) and the earlier candle-SL retest.
Run: python backtest_vwap_retest_vwapsl.py
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
        use_vwap_sl = (kind == "RETEST" and vwap_buf is not None)
        if d == "BUY":
            sl = (b["vwap"] - vwap_buf) if use_vwap_sl else (b["low"] - SL_BUFFER)
            if entry <= sl:
                continue
            risk = entry - sl
            tp = entry + TP_MULT * risk
        else:
            sl = (b["vwap"] + vwap_buf) if use_vwap_sl else (b["high"] + SL_BUFFER)
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
        print("=" * 172)
        print(f"{name}: {len(bars)} bars")
        print("=" * 172)
        base = execute(bars, ts, px, make_signals(bars, 0.0, True, False))
        print(row("BASELINE: first-close only", stats(base)))
        for label, tol in (("A strict touch", 0.0), ("B within $0.50", 0.5)):
            sigs_combo = make_signals(bars, tol, True, True)
            sigs_only = make_signals(bars, tol, False, True)
            candle = execute(bars, ts, px, sigs_combo, None)
            print(f"  -- Retest {label}")
            print(row("  candle-SL (earlier): combined", stats(candle)))
            print(row("     retest entries only", stats([x for x in candle if x["kind"] == "RETEST"])))
            for buf in (0.5, 1.0, 1.5):
                combo = execute(bars, ts, px, sigs_combo, buf)
                only = execute(bars, ts, px, sigs_only, buf)
                print(row(f"  VWAP-SL buf ${buf:.2f}: combined", stats(combo)))
                print(row("     retest entries (in combined)", stats([x for x in combo if x["kind"] == "RETEST"])))
                print(row("     RETEST-ONLY run", stats(only)))
        print()


if __name__ == "__main__":
    main()
