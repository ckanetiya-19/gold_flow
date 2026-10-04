"""
Standalone backtest (read-only): VWAP first-close + VWAP RETEST entries, FULL period only.

Base (unchanged): daily-reset VWAP; first GREEN candle closing above VWAP = BUY, first RED candle
closing below VWAP = SELL (one per stretch); SL = candle low -$1 / high +$1; TP = 2 x risk;
1:1 break-even (SL -> entry +/- $0.05); one position at a time; entry = first tick of next candle;
tick-level exits; spread $0.24 per trade; market hours only.

RETEST (new), user defaults:
  BUY : in an above-VWAP stretch, a GREEN candle that closes above VWAP AND whose low reaches down to
        VWAP (A: low <= VWAP, strict touch)  or  to within $0.50 of VWAP (B: low <= VWAP + 0.50),
        and the PREVIOUS candle did not touch (= a fresh pullback, not price hugging VWAP).
  SELL: mirror (RED candle closing below VWAP, high >= VWAP  /  high >= VWAP - 0.50).
  Every fresh retest is a signal (as many as occur); same SL/TP/BE rules.
Runs: baseline (first-close only) | first-close + retest A | first-close + retest B | retest-only A | retest-only B
Run: python backtest_vwap_retest.py
"""
from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars, market_closed
from backtest_vwap_first_close_be import run_exit_be

SPREAD = 0.24
SL_BUFFER = 1.0
TP_MULT = 2.0


def make_signals(bars, tol, use_first, use_retest):
    sigs = []
    side, used = None, False
    for i in range(len(bars) - 1):
        b = bars[i]
        if b["close"] > b["vwap"]:
            s = "A"
        elif b["close"] < b["vwap"]:
            s = "B"
        else:
            s = side
        if s != side:
            side, used = s, False
        green, red = b["close"] > b["open"], b["close"] < b["open"]
        # first-close (once per stretch)
        if not used:
            if side == "A" and green:
                used = True
                if use_first:
                    sigs.append((i, "BUY", "FIRST"))
                continue
            if side == "B" and red:
                used = True
                if use_first:
                    sigs.append((i, "SELL", "FIRST"))
                continue
        # retest (fresh touch of VWAP inside the same stretch, after the first-close)
        if use_retest and i > 0:
            p = bars[i - 1]
            if side == "A" and green and b["low"] <= b["vwap"] + tol and not (p["low"] <= p["vwap"] + tol):
                sigs.append((i, "BUY", "RETEST"))
            elif side == "B" and red and b["high"] >= b["vwap"] - tol and not (p["high"] >= p["vwap"] - tol):
                sigs.append((i, "SELL", "RETEST"))
    return sigs


def execute(bars, ts, px, sigs):
    trades, free_idx = [], -1
    for i, d, kind in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        if d == "BUY":
            sl = b["low"] - SL_BUFFER
            if entry <= sl:
                continue
            risk = entry - sl
            tp = entry + TP_MULT * risk
        else:
            sl = b["high"] + SL_BUFFER
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


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    w = sum(1 for x in tr if x["why"] == "WIN")
    be = sum(1 for x in tr if x["why"] == "BE")
    l = sum(1 for x in tr if x["why"] == "LOSS")
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, w=w, be=be, l=l, gross=gross, net=net, avg=net / n, dd=dd,
                risk=sum(x["risk"] for x in tr) / n)


def row(label, s):
    if s is None:
        return f"   {label:34s}: no trades"
    return (f"   {label:34s}: trades {s['n']:4d} | W:{s['w']:3d} BE:{s['be']:3d} L:{s['l']:3d} | avg SL ${s['risk']:.2f} | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, FULL period only, spread ${SPREAD}, TP=2xSL, 1:1 BE\n")
    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        print("=" * 170)
        print(f"{name}: {len(bars)} bars")
        print("=" * 170)
        base = execute(bars, ts, px, make_signals(bars, 0.0, True, False))
        print(row("BASELINE: first-close only", stats(base)))
        for label, tol in (("A strict touch", 0.0), ("B within $0.50", 0.5)):
            combo = execute(bars, ts, px, make_signals(bars, tol, True, True))
            print(row(f"first-close + RETEST {label}", stats(combo)))
            print(row("   of which FIRST entries", stats([x for x in combo if x["kind"] == "FIRST"])))
            print(row("   of which RETEST entries", stats([x for x in combo if x["kind"] == "RETEST"])))
            only = execute(bars, ts, px, make_signals(bars, tol, False, True))
            print(row(f"RETEST-ONLY {label}", stats(only)))
            print(row("   BUY retests", stats([x for x in only if x["d"] == "BUY"])))
            print(row("   SELL retests", stats([x for x in only if x["d"] == "SELL"])))
        print()


if __name__ == "__main__":
    main()
