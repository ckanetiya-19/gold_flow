"""
Standalone backtest (read-only): FIRST-CLOSE entries with a TRAILING STOP instead of a fixed 2xSL TP.
  Entry/initial SL: unchanged (candle-SL = low -$1 / high +$1 ; VWAP-SL = VWAP -/+ $0.50).
  Trailing rule (user-specified): once floating profit reaches TRAIL_ARM ($2), the stop trails
  TRAIL_DIST ($1) behind the best price reached since entry (only tightens, never loosens).
  No fixed take-profit - trade runs until the trailing stop (or the original SL, before arming) is hit.
  Tick-level exits, spread $0.24 per trade, market hours only, FULL period.
Compared against the fixed-2xSL-TP+1:1-BE version already running live (V1 candle-SL, V4 VWAP-SL).
Run: python backtest_vwap_trailing.py
"""
from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars, market_closed
from backtest_vwap_retest import make_signals
from backtest_vwap_first_close_be import execute as ex_be

TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
SPREAD = 0.24
SL_BUFFER = 1.0


def run_trailing_exit(ts, px, start, d, entry, sl0):
    long_ = d == "BUY"
    best = entry
    sl = sl0
    armed = False
    for k in range(start, len(ts)):
        p = px[k]
        if long_:
            best = max(best, p)
            if not armed and (best - entry) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = max(sl, best - TRAIL_DIST)
            if p <= sl:
                return sl, k, ("TRAIL" if armed else "LOSS")
        else:
            best = min(best, p)
            if not armed and (entry - best) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = min(sl, best + TRAIL_DIST)
            if p >= sl:
                return sl, k, ("TRAIL" if armed else "LOSS")
    return px[-1], len(ts) - 1, "OPEN"


def execute_trailing(bars, ts, px, sigs, vwap_buf=None):
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
        else:
            sl = (b["vwap"] + vwap_buf) if vwap_buf is not None else (b["high"] + SL_BUFFER)
            if entry >= sl:
                continue
        xp, xi, why = run_trailing_exit(ts, px, nxt, d, entry, sl)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "risk": abs(entry - sl), "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    w = sum(1 for x in tr if x["why"] == "TRAIL")
    l = sum(1 for x in tr if x["why"] == "LOSS")
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, w=w, l=l, gross=gross, net=net, avg=net / n, dd=dd, risk=sum(x["risk"] for x in tr) / n)


def row(label, s):
    if s is None:
        return f"   {label:32s}: no trades"
    return (f"   {label:32s}: trades {s['n']:4d} | trail-exit:{s['w']:3d} loss(pre-arm):{s['l']:3d} | avg SL ${s['risk']:.2f} | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, FULL period, spread ${SPREAD}, trail arm ${TRAIL_ARM} / dist ${TRAIL_DIST}\n")
    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        sigs = make_signals(bars, 0.0, True, False)
        print("=" * 150)
        print(f"{name}: {len(bars)} bars | {len(sigs)} first-close signals")
        print("=" * 150)

        base_v1 = ex_be(bars, ts, px, [(i, d) for i, d, k in sigs], use_be=True)
        row_v1 = {"n": len(base_v1), "w": sum(1 for x in base_v1 if x["why"] == "WIN"),
                  "l": sum(1 for x in base_v1 if x["why"] in ("LOSS", "BE")),
                  "gross": sum(x["pnl"] for x in base_v1), "net": sum(x["pnl"] for x in base_v1) - SPREAD * len(base_v1),
                  "avg": 0, "dd": 0, "risk": sum(x["risk"] for x in base_v1) / max(1, len(base_v1))}
        row_v1["avg"] = row_v1["net"] / max(1, row_v1["n"])
        print(row("BASELINE V1: candle-SL, 2xSL TP +BE", row_v1))
        print(row("TRAILING: candle-SL, $2 arm/$1 trail", stats(execute_trailing(bars, ts, px, sigs))))
        print(row("TRAILING: VWAP-SL $0.50, $2 arm/$1 trail", stats(execute_trailing(bars, ts, px, sigs, 0.5))))
        for buy_only, sell_only in [(True, False), (False, True)]:
            label = "BUY only" if buy_only else "SELL only"
            f = [s for s in sigs if s[1] == ("BUY" if buy_only else "SELL")]
            print(row(f"   TRAILING candle-SL {label}", stats(execute_trailing(bars, ts, px, f))))
        print()


if __name__ == "__main__":
    main()
