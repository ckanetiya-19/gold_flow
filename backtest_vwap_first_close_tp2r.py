"""
Standalone backtest (read-only): same "VWAP first-close" rule as
backtest_vwap_first_close.py, but TP = 2 x (SL distance) instead of fixed $2.
  SL = signal candle low - $1 (BUY) / high + $1 (SELL)   [unchanged]
  risk = |entry - SL|;  TP = entry +/- 2 x risk  (risk $3 -> TP $6, risk $5 -> TP $10)
Everything else identical: daily-reset VWAP, one trade per stretch, one position at a
time, tick-level exits, spread $0.24 per trade, market-hours only, 67/33 split,
random-entry baseline using the same SL/TP engine.
Run: python backtest_vwap_first_close_tp2r.py
"""
import random
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, run_exit, pnl_of
from backtest_vwap_first_close import build_bars, make_signals, market_closed

SPREAD = 0.24
SL_BUFFER = 1.0
TP_MULT = 2.0


def execute(bars, ts, px, sigs, market_only=True):
    trades = []
    free_idx = -1
    for i, d in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx:
            continue
        if market_only and market_closed(ts[nxt]):
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
        xp, xi, why = run_exit(ts, px, nxt, d, sl, tp)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "te": ts[xi], "d": d, "risk": risk, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    tp = sum(1 for x in tr if x["why"] == "TP")
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    risk = sum(x["risk"] for x in tr) / n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    # p*(2r - s) = (1-p)*(r + s)  ->  p = (r + s) / (3r)
    be_wr = (risk + SPREAD) / ((1 + TP_MULT) * risk) * 100
    hold = sum(x["te"] - x["t"] for x in tr) / n / 60
    return dict(n=n, wr=tp / n * 100, risk=risk, gross=gross, net=net, dd=dd, avg=net / n, be=be_wr, hold=hold)


def line(label, s):
    if s is None:
        return f"   {label:11s}: no trades"
    return (f"   {label:11s}: trades {s['n']:4d} | WR {s['wr']:5.1f}% (breakeven {s['be']:4.1f}%) | avg SL ${s['risk']:.2f} -> TP ${s['risk']*TP_MULT:.2f} | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}% | avg hold {s['hold']:.0f} min")


def main():
    ts, px, vol, buy = load_ticks()
    split_t = ts[0] + (ts[-1] - ts[0]) * 0.67
    print(f"{(ts[-1]-ts[0])/86400:.2f} days; split at {datetime.fromtimestamp(split_t, timezone.utc)}\n")
    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        sigs = make_signals(bars)
        tr = execute(bars, ts, px, sigs)
        print("=" * 160)
        print(f"{name}: {len(bars)} bars | {len(sigs)} first-close signals | {len(tr)} trades taken | TP = {TP_MULT}x SL")
        print("=" * 160)
        print(line("FULL", stats(tr)))
        print(line("IN-SAMPLE", stats([x for x in tr if x["t"] < split_t])))
        print(line("OUT-SAMPLE", stats([x for x in tr if x["t"] >= split_t])))
        print(line("BUY only", stats([x for x in tr if x["d"] == "BUY"])))
        print(line("SELL only", stats([x for x in tr if x["d"] == "SELL"])))
        print(line("all hours*", stats(execute(bars, ts, px, sigs, market_only=False))) + "  (*incl. weekend PAXG-only)")
        full, oos = [], []
        for seed in range(30):
            rnd = random.Random(seed)
            idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(len(sigs), len(bars) - 40)))
            rs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
            rt = execute(bars, ts, px, rs)
            sf = stats(rt); so = stats([x for x in rt if x["t"] >= split_t])
            full.append(sf["net"] if sf else 0.0); oos.append(so["net"] if so else 0.0)
        print(f"   RANDOM baseline (30 seeds, same engine): FULL net mean ${sum(full)/len(full):8.2f} range [${min(full):.2f} .. ${max(full):.2f}]"
              f" | OOS net mean ${sum(oos)/len(oos):7.2f} range [${min(oos):.2f} .. ${max(oos):.2f}]\n")


if __name__ == "__main__":
    main()
