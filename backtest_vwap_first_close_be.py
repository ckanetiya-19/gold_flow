"""
Standalone backtest (read-only): "VWAP first-close" rule, TP = 2 x SL, WITH 1:1 break-even.
  SL = signal candle low - $1 (BUY) / high + $1 (SELL);  risk = |entry - SL|;  TP = entry +/- 2 x risk
  BREAK-EVEN: once price reaches entry +/- 1 x risk (1:1), SL moves to entry +/- $0.05.
  Exits on the real tick path. Outcomes reported as 3 honest buckets:
     WIN (TP hit) | BE (stopped at moved SL after 1:1 was reached) | LOSS (original SL hit)
  Spread $0.24 per trade, market hours only. Compared side by side with the NO-BE version.
Run: python backtest_vwap_first_close_be.py
"""
import random

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars, make_signals, market_closed

SPREAD = 0.24
SL_BUFFER = 1.0
TP_MULT = 2.0
BE_TRIGGER_R = 1.0
BE_BUFFER = 0.05


def run_exit_be(ts, px, start, d, entry, sl, tp, risk, use_be):
    long_ = d == "BUY"
    be_price = entry + BE_TRIGGER_R * risk if long_ else entry - BE_TRIGGER_R * risk
    cur_sl = sl
    armed = False
    for k in range(start, len(ts)):
        p = px[k]
        if use_be and not armed and (p >= be_price if long_ else p <= be_price):
            armed = True
            cur_sl = entry + BE_BUFFER if long_ else entry - BE_BUFFER
        hit_sl = p <= cur_sl if long_ else p >= cur_sl
        hit_tp = p >= tp if long_ else p <= tp
        if hit_sl:
            return cur_sl, k, ("BE" if armed else "LOSS")
        if hit_tp:
            return tp, k, "WIN"
    return px[-1], len(ts) - 1, "OPEN"


def execute(bars, ts, px, sigs, use_be):
    trades, free_idx = [], -1
    for i, d in sigs:
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
        xp, xi, why = run_exit_be(ts, px, nxt, d, entry, sl, tp, risk, use_be)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "te": ts[xi], "d": d, "risk": risk, "pnl": pnl_of(d, entry, xp), "why": why})
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
    return dict(n=n, w=w, be=be, l=l, wr=w / n * 100, gross=gross, net=net, dd=dd, avg=net / n,
                risk=sum(x["risk"] for x in tr) / n, hold=sum(x["te"] - x["t"] for x in tr) / n / 60)


def line(label, s):
    if s is None:
        return f"   {label:14s}: no trades"
    return (f"   {label:14s}: trades {s['n']:3d} | W:{s['w']:2d} BE:{s['be']:2d} L:{s['l']:2d} | WinRate(TP only) {s['wr']:5.1f}% | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}% | hold {s['hold']:.0f} min")


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, FULL period only\n")
    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        sigs = make_signals(bars)
        no_be = execute(bars, ts, px, sigs, use_be=False)
        with_be = execute(bars, ts, px, sigs, use_be=True)
        print("=" * 175)
        print(f"{name}: {len(bars)} bars | {len(sigs)} first-close signals | TP = 2x SL")
        print("=" * 175)
        print(line("NO break-even", stats(no_be)))
        print(line("WITH 1:1 BE", stats(with_be)))
        print(line("  BE: BUY only", stats([x for x in with_be if x["d"] == "BUY"])))
        print(line("  BE: SELL only", stats([x for x in with_be if x["d"] == "SELL"])))
        rnd_net = []
        for seed in range(30):
            rnd = random.Random(seed)
            idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(len(sigs), len(bars) - 40)))
            rs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
            s = stats(execute(bars, ts, px, rs, use_be=True))
            rnd_net.append(s["net"] if s else 0.0)
        print(f"   RANDOM (with BE, 30 seeds): net mean ${sum(rnd_net)/len(rnd_net):.2f}  range [${min(rnd_net):.2f} .. ${max(rnd_net):.2f}]\n")


if __name__ == "__main__":
    main()
