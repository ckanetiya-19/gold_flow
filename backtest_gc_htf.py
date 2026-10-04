"""
LOCAL ONLY (no API, no cost): test the order-flow strategies that BEAT RANDOM
on 1-min (Delta Exhaustion, Delta Momentum) - plus CVD Divergence/Trend - on
HIGHER timeframes (5m, 15m), where the real GC spread ($0.31) is a much
smaller fraction of the ATR target. If the real-order-flow signal (which
beat random on 1m but lost to spread) survives here into positive net/trade
AND still beats random, that's a genuine edge. If not, the honest conclusion
is mechanical order-flow scalping on gold has no tradeable edge with this
data/approach.

Uses cached gc_bars.pkl / gc_ticks.npz (real aggressor side, A=BUY/B=SELL).
Run: python backtest_gc_htf.py
"""
import pickle
import numpy as np
import random
import datetime

GC_SPREAD = 0.31
ATR_PERIOD = 14
TIME_STOP_BARS = 48   # per-TF bars (4h on 5m, 12h on 15m)
random.seed(2024)


def load():
    with open("gc_bars.pkl", "rb") as f:
        bars = pickle.load(f)
    z = np.load("gc_ticks.npz")
    return bars, z["ts"], z["px"]


def resample(bars, tf_min):
    """Aggregate 1-min bars into tf_min buckets. Keeps real summed buy/sell
    volume + delta; cvd = last cumulative value in the bucket; next_tick =
    the last constituent's next_tick (first tick after the HTF bar closes)."""
    out = []
    cur = None
    for b in bars:
        bucket = int(b["t"] // (tf_min * 60)) * (tf_min * 60)
        if cur is None or cur["t"] != bucket:
            if cur is not None:
                out.append(cur)
            cur = {"t": bucket, "open": b["open"], "high": b["high"], "low": b["low"],
                   "close": b["close"], "volume": b["volume"], "buy_vol": b["buy_vol"],
                   "sell_vol": b["sell_vol"], "cvd": b["cvd"], "next_tick": b["next_tick"]}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
            cur["buy_vol"] += b["buy_vol"]
            cur["sell_vol"] += b["sell_vol"]
            cur["cvd"] = b["cvd"]
            cur["next_tick"] = b["next_tick"]
    if cur is not None:
        out.append(cur)
    for b in out:
        b["delta"] = b["buy_vol"] - b["sell_vol"]
    return out


def calc_atr(bars, period=ATR_PERIOD):
    atr = [2.0] * len(bars)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr[i] = sum(trs[-period:]) / len(trs[-period:])
    return atr


def market_closed(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    wd, hr = d.weekday(), d.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


def simulate(bars, ts, px, sigs, atr, sl_mult, tp_mult, tf_min, spread=GC_SPREAD):
    trades, free_tick, n = [], -1, len(px)
    for i, d in sorted(sigs, key=lambda s: s[0]):
        nxt = bars[i]["next_tick"]
        if nxt <= free_tick or nxt >= n or market_closed(ts[nxt]):
            continue
        a = atr[i]
        if a <= 0:
            continue
        entry = px[nxt]
        if d == "BUY":
            sl, tp = entry - sl_mult * a, entry + tp_mult * a
        else:
            sl, tp = entry + sl_mult * a, entry - tp_mult * a
        stop_ts = ts[nxt] + TIME_STOP_BARS * tf_min * 60
        xp, xtick, why = entry, nxt, "TIME"
        for k in range(nxt, n):
            p = px[k]
            if d == "BUY":
                if p <= sl: xp, xtick, why = sl, k, "SL"; break
                if p >= tp: xp, xtick, why = tp, k, "TP"; break
            else:
                if p >= sl: xp, xtick, why = sl, k, "SL"; break
                if p <= tp: xp, xtick, why = tp, k, "TP"; break
            if ts[k] >= stop_ts:
                xp, xtick, why = p, k, "TIME"; break
        gross = (xp - entry) if d == "BUY" else (entry - xp)
        trades.append({"t": ts[nxt], "pnl": gross - spread, "why": why})
        free_tick = xtick
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    wins = sum(1 for x in tr if x["pnl"] > 0)
    net = sum(x["pnl"] for x in tr)
    eq = peak = dd = 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return dict(n=n, wr=wins / n * 100, net=net, avg=net / n, dd=dd)


def row(label, s):
    if s is None:
        return f"   {label:36s}: no trades"
    return (f"   {label:36s}: trades {s['n']:5d} | WR {s['wr']:5.1f}% | "
            f"NET ${s['net']:8.1f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:7.1f}")


def sig_delta_exhaustion(bars, look=20, pct=0.9):
    deltas = np.array([abs(b["delta"]) for b in bars])
    sigs = []
    for i in range(look + 1, len(bars)):
        base = deltas[max(0, i - 100):i]
        thresh = np.quantile(base, pct) if len(base) else 0
        d = bars[i]["delta"]
        if abs(d) < thresh:
            continue
        highs = [bars[j]["high"] for j in range(i - look, i)]
        lows = [bars[j]["low"] for j in range(i - look, i)]
        if d > 0 and bars[i]["high"] > max(highs):
            sigs.append((i, "SELL"))
        elif d < 0 and bars[i]["low"] < min(lows):
            sigs.append((i, "BUY"))
    return sigs


def sig_delta_momentum(bars, look=50, pct=0.8):
    deltas = np.array([abs(b["delta"]) for b in bars])
    sigs = []
    for i in range(look + 1, len(bars)):
        thresh = np.quantile(deltas[i - look:i], pct)
        d = bars[i]["delta"]
        if abs(d) < thresh:
            continue
        if d > 0 and bars[i]["close"] > bars[i]["open"]:
            sigs.append((i, "BUY"))
        elif d < 0 and bars[i]["close"] < bars[i]["open"]:
            sigs.append((i, "SELL"))
    return sigs


def sig_cvd_divergence(bars, look=10):
    cvd = np.array([b["cvd"] for b in bars])
    sigs = []
    for i in range(look + 1, len(bars)):
        lows = [bars[j]["low"] for j in range(i - look, i)]
        highs = [bars[j]["high"] for j in range(i - look, i)]
        if bars[i]["low"] < min(lows) and cvd[i] > min(cvd[i - look:i]):
            sigs.append((i, "BUY"))
        elif bars[i]["high"] > max(highs) and cvd[i] < max(cvd[i - look:i]):
            sigs.append((i, "SELL"))
    return sigs


def random_sigs(bars, n, seed):
    rnd = random.Random(seed)
    idxs = sorted(rnd.sample(range(30, len(bars) - 1), min(n, len(bars) - 40)))
    return [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]


def main():
    print("Loading cached GC bars/ticks (local, no cost)...")
    bars1, ts, px = load()
    print(f"  {len(bars1):,} 1-min bars | {len(px):,} ticks\n")

    for tf in (5, 15):
        bars = resample(bars1, tf)
        atr = calc_atr(bars)
        print("#" * 108)
        print(f"# {tf}-MINUTE TIMEFRAME  ({len(bars):,} bars)")
        print("#" * 108)
        strategies = {
            "Delta Exhaustion (fade)": sig_delta_exhaustion(bars),
            "Delta Momentum (follow)": sig_delta_momentum(bars),
            "CVD Divergence (mean-rev)": sig_cvd_divergence(bars),
        }
        for sname, sigs in strategies.items():
            print(f"\n{sname}  ({len(sigs)} signals)")
            for sl_m, tp_m in [(1.0, 2.0), (1.0, 1.5), (1.5, 3.0)]:
                s = stats(simulate(bars, ts, px, sigs, atr, sl_m, tp_m, tf))
                print(row(f"SL{sl_m}/TP{tp_m}xATR", s))
                if s:
                    rs = stats(simulate(bars, ts, px, random_sigs(bars, len(sigs), seed=int(sl_m*100+tp_m*7)+tf), atr, sl_m, tp_m, tf))
                    print(row(f"   -> random", rs))
        print()

    print("=" * 108)
    print("RULE: real edge only if net/trade > 0 AND clearly beats its random row.")
    print("=" * 108)


if __name__ == "__main__":
    main()
