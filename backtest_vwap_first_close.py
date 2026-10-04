"""
Standalone backtest (read-only, does NOT touch any live port or MT5):
"VWAP first-close" strategy, user-specified rule.

Rule (per timeframe: 1-min and 5-min):
  * VWAP = daily-reset (00:00 UTC) volume-weighted average price (yellow line).
  * A "stretch" = consecutive candle closes on the same side of VWAP.
  * BUY : first GREEN candle that closes ABOVE VWAP in an above-stretch.
          Enter at the OPEN (first tick) of the next candle.
          SL = that candle's LOW - $1.00,  TP = entry + $2.00 (fixed).
  * SELL: first RED candle that closes BELOW VWAP in a below-stretch.
          SL = that candle's HIGH + $1.00, TP = entry - $2.00.
  * Only ONE trade per stretch, even if TP/SL was hit and price keeps going.
    A new trade needs price to cross to the other side and a new first signal.
  * One position at a time: a signal that arrives while a trade is open is
    skipped and the stretch counts as used.
  * Exits are checked on the real tick path (SL wins a tie), spread $0.24
    subtracted per trade (also shown gross), PnL = $ per 0.01 lot.
  * Signals during spot-gold market closure (Fri 21:00 - Sun 22:00 UTC) are skipped
    (Binance PAXG trades 24/7, real XAUUSD does not).

Also: 67/33 in-sample / out-of-sample split by time, random-entry baseline
with the same SL/TP/one-at-a-time engine, and breakeven win-rate.

Run: python backtest_vwap_first_close.py
"""

import random
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, run_exit, pnl_of

SPREAD = 0.24
SL_BUFFER = 1.0
TP_DIST = 2.0


def build_bars(ts, px, vol, bucket_s):
    bars = []
    cur = None
    cum_pv = cum_vol = 0.0
    cur_day = None
    for i in range(len(ts)):
        bucket = int(ts[i] // bucket_s) * bucket_s
        if cur is not None and bucket > cur["t"]:
            cur["vwap"] = cum_pv / cum_vol if cum_vol > 0 else cur["close"]
            cur["next_tick"] = i
            bars.append(cur)
            cur = None
        day = int(ts[i] // 86400)
        if day != cur_day:
            cum_pv = cum_vol = 0.0
            cur_day = day
        cum_pv += px[i] * vol[i]
        cum_vol += vol[i]
        if cur is None:
            cur = {"t": bucket, "open": px[i], "high": px[i], "low": px[i], "close": px[i]}
        else:
            cur["high"] = max(cur["high"], px[i])
            cur["low"] = min(cur["low"], px[i])
            cur["close"] = px[i]
    return bars


def market_closed(t):
    d = datetime.fromtimestamp(t, timezone.utc)
    wd, hr = d.weekday(), d.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


def make_signals(bars):
    sigs = []
    side = None
    used = False
    for i in range(len(bars) - 1):
        b = bars[i]
        if b["close"] > b["vwap"]:
            s = "A"
        elif b["close"] < b["vwap"]:
            s = "B"
        else:
            s = side
        if s != side:
            side = s
            used = False
        if used:
            continue
        if side == "A" and b["close"] > b["open"]:
            sigs.append((i, "BUY")); used = True
        elif side == "B" and b["close"] < b["open"]:
            sigs.append((i, "SELL")); used = True
    return sigs


def execute(bars, ts, px, sigs, market_only=True):
    trades = []
    free_idx = -1
    for i, d in sigs:
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx:
            continue                      # position still open -> skip
        if market_only and market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        if d == "BUY":
            sl, tp = b["low"] - SL_BUFFER, entry + TP_DIST
            if entry <= sl:
                continue
        else:
            sl, tp = b["high"] + SL_BUFFER, entry - TP_DIST
            if entry >= sl:
                continue
        xp, xi, why = run_exit(ts, px, nxt, d, sl, tp)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "risk": abs(entry - sl), "pnl": pnl_of(d, entry, xp), "why": why})
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
    be_wr = (risk + SPREAD) / (TP_DIST + risk) * 100
    return dict(n=n, wr=tp / n * 100, risk=risk, gross=gross, net=net, dd=dd, avg=net / n, be=be_wr)


def line(label, s):
    if s is None:
        return f"   {label:11s}: no trades"
    return (f"   {label:11s}: trades {s['n']:4d} | WR {s['wr']:5.1f}% (breakeven WR {s['be']:4.1f}%) | avg risk ${s['risk']:.2f} | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


def main():
    ts, px, vol, buy = load_ticks()
    split_t = ts[0] + (ts[-1] - ts[0]) * 0.67
    print(f"{(ts[-1]-ts[0])/86400:.2f} days; split at {datetime.fromtimestamp(split_t, timezone.utc)}\n")

    for name, bucket_s in (("1-MIN", 60), ("5-MIN", 300)):
        bars = build_bars(ts, px, vol, bucket_s)
        sigs = make_signals(bars)
        tr = execute(bars, ts, px, sigs, market_only=True)
        tr_all = execute(bars, ts, px, sigs, market_only=False)
        print("=" * 150)
        print(f"{name}: {len(bars)} bars | {len(sigs)} first-close signals | {len(tr)} trades taken (market hours only)")
        print("=" * 150)
        print(line("FULL", stats(tr)))
        print(line("IN-SAMPLE", stats([x for x in tr if x["t"] < split_t])))
        print(line("OUT-SAMPLE", stats([x for x in tr if x["t"] >= split_t])))
        buys = [x for x in tr if x["d"] == "BUY"]; sells = [x for x in tr if x["d"] == "SELL"]
        print(line("BUY only", stats(buys)))
        print(line("SELL only", stats(sells)))
        print(line("all hours*", stats(tr_all)) + "   (*incl. weekend PAXG-only moves)")

        # random baseline: same number of signals, random bars/directions, same engine
        n_sig = len(sigs)
        full, oos = [], []
        for seed in range(30):
            rnd = random.Random(seed)
            idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(n_sig, len(bars) - 40)))
            rs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
            rt = execute(bars, ts, px, rs, market_only=True)
            s_full = stats(rt); s_oos = stats([x for x in rt if x["t"] >= split_t])
            full.append(s_full["net"] if s_full else 0.0)
            oos.append(s_oos["net"] if s_oos else 0.0)
        print(f"   RANDOM baseline (30 seeds, same engine): FULL net mean ${sum(full)/len(full):8.2f} range [${min(full):.2f} .. ${max(full):.2f}]"
              f" | OOS net mean ${sum(oos)/len(oos):7.2f} range [${min(oos):.2f} .. ${max(oos):.2f}]\n")


if __name__ == "__main__":
    main()
