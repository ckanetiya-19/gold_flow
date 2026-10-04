"""
LOCAL ONLY (no API, no cost): backtest the 4 VWAP strategies from the slides
on REAL CME gold-futures data (cached gc_bars.pkl / gc_ticks.npz).

BACKTEST-ONLY ADAPTATIONS (the slides are designed for stocks with a
session/daily-anchored VWAP; our live system uses a rolling VWAP and does NOT
have 2SD/3SD bands - none of this touches live code):
  - VWAP re-anchored EACH UTC DAY (session VWAP), with 1/2/3-SD bands
    (volume-weighted std of typical price since the day's anchor).
  - 5-minute bars (the slides are all 5-min charts).
  - Faithful entry rules per slide; exits normalized to ATR so every
    strategy is directly comparable to its random-entry control (the clean
    way to isolate whether the ENTRY has edge). The slides' structural stops
    (below candle / % of VWAP) are a refinement, not tested here - the
    question is only "does the signal beat random?".

GC real spread $0.31. PnL in price points. Random-entry control on each.

Run: python backtest_gc_vwap4.py
"""
import pickle
import numpy as np
import random
import datetime

GC_SPREAD = 0.31
ATR_PERIOD = 14
TIME_STOP_BARS = 48
random.seed(77)


def load():
    with open("gc_bars.pkl", "rb") as f:
        bars = pickle.load(f)
    z = np.load("gc_ticks.npz")
    return bars, z["ts"], z["px"]


def resample(bars, tf_min):
    out, cur = [], None
    for b in bars:
        bucket = int(b["t"] // (tf_min * 60)) * (tf_min * 60)
        if cur is None or cur["t"] != bucket:
            if cur is not None:
                out.append(cur)
            cur = {"t": bucket, "open": b["open"], "high": b["high"], "low": b["low"],
                   "close": b["close"], "volume": b["volume"], "buy_vol": b["buy_vol"],
                   "sell_vol": b["sell_vol"], "cvd": b["cvd"], "next_tick": b["next_tick"]}
        else:
            cur["high"] = max(cur["high"], b["high"]); cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]; cur["volume"] += b["volume"]
            cur["buy_vol"] += b["buy_vol"]; cur["sell_vol"] += b["sell_vol"]
            cur["cvd"] = b["cvd"]; cur["next_tick"] = b["next_tick"]
    if cur is not None:
        out.append(cur)
    for b in out:
        b["delta"] = b["buy_vol"] - b["sell_vol"]
    return out


def daily_vwap_bands(bars):
    """Session VWAP re-anchored each UTC day + 1/2/3 SD bands, and a
    bars-since-anchor counter."""
    vwap = np.zeros(len(bars)); sd = np.zeros(len(bars)); since = np.zeros(len(bars), dtype=int)
    cur_day = None
    cum_pv = cum_v = cum_pv2 = 0.0
    n = 0
    for i, b in enumerate(bars):
        day = datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc).date()
        if day != cur_day:
            cur_day = day; cum_pv = cum_v = cum_pv2 = 0.0; n = 0
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b["volume"] or 1e-9
        cum_pv += tp * v; cum_v += v; cum_pv2 += tp * tp * v
        vw = cum_pv / cum_v
        var = max(0.0, cum_pv2 / cum_v - vw * vw)
        vwap[i] = vw; sd[i] = var ** 0.5; since[i] = n
        n += 1
    return vwap, sd, since


def calc_atr(bars, period=ATR_PERIOD):
    atr = [2.0] * len(bars); trs = []
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
        sl = entry - sl_mult * a if d == "BUY" else entry + sl_mult * a
        tp = entry + tp_mult * a if d == "BUY" else entry - tp_mult * a
        stop_ts = ts[nxt] + TIME_STOP_BARS * tf_min * 60
        xp, xtick = entry, nxt
        for k in range(nxt, n):
            p = px[k]
            if d == "BUY":
                if p <= sl: xp, xtick = sl, k; break
                if p >= tp: xp, xtick = tp, k; break
            else:
                if p >= sl: xp, xtick = sl, k; break
                if p <= tp: xp, xtick = tp, k; break
            if ts[k] >= stop_ts:
                xp, xtick = p, k; break
        gross = (xp - entry) if d == "BUY" else (entry - xp)
        trades.append({"t": ts[nxt], "pnl": gross - spread})
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
        return f"   {label:34s}: no trades"
    return (f"   {label:34s}: trades {s['n']:5d} | WR {s['wr']:5.1f}% | "
            f"NET ${s['net']:8.1f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:7.1f}")


# ------------------------------------------------------------------ 4 strategies
def s1_bounce(bars, vwap, sd, since):
    """Trend continuation: price above VWAP for 30+min (6 bars), pulls back to
    touch VWAP on lower volume, bullish candle bounces. Mirror for shorts."""
    sigs = []
    for i in range(8, len(bars)):
        b = bars[i]
        up_run = sum(1 for j in range(i - 6, i) if bars[j]["close"] > vwap[j])
        dn_run = sum(1 for j in range(i - 6, i) if bars[j]["close"] < vwap[j])
        avgvol = np.mean([bars[j]["volume"] for j in range(i - 6, i)])
        if up_run >= 5 and b["low"] <= vwap[i] and b["close"] > vwap[i] and b["close"] > b["open"] and b["volume"] < avgvol:
            sigs.append((i, "BUY"))
        elif dn_run >= 5 and b["high"] >= vwap[i] and b["close"] < vwap[i] and b["close"] < b["open"] and b["volume"] < avgvol:
            sigs.append((i, "SELL"))
    return sigs


def s2_reclaim(bars, vwap, sd, since):
    """Reversal: 15+min (3 bars) below VWAP, strong candle reclaims above on
    above-avg volume with body >=half above the line. Mirror for shorts."""
    sigs = []
    for i in range(6, len(bars)):
        b = bars[i]
        body = abs(b["close"] - b["open"]) or 1e-9
        avgvol = np.mean([bars[j]["volume"] for j in range(i - 5, i)])
        below3 = all(bars[j]["close"] < vwap[j] for j in range(i - 3, i))
        above3 = all(bars[j]["close"] > vwap[j] for j in range(i - 3, i))
        if below3 and b["close"] > vwap[i] and b["volume"] > avgvol and (b["close"] - vwap[i]) >= 0.5 * body:
            sigs.append((i, "BUY"))
        elif above3 and b["close"] < vwap[i] and b["volume"] > avgvol and (vwap[i] - b["close"]) >= 0.5 * body:
            sigs.append((i, "SELL"))
    return sigs


def s3_deviation_scalp(bars, vwap, sd, since):
    """Mean reversion: tag +/-2SD, reversal candle, declining volume, skip
    first 30min (6 bars since anchor)."""
    sigs = []
    for i in range(2, len(bars)):
        if since[i] < 6 or sd[i] <= 0:
            continue
        b = bars[i]
        upper2, lower2 = vwap[i] + 2 * sd[i], vwap[i] - 2 * sd[i]
        vol_decl = b["volume"] < bars[i - 1]["volume"]
        if b["low"] <= lower2 and b["close"] > b["open"] and vol_decl:
            sigs.append((i, "BUY"))
        elif b["high"] >= upper2 and b["close"] < b["open"] and vol_decl:
            sigs.append((i, "SELL"))
    return sigs


def s4_exhaustion(bars, vwap, sd, since):
    """Fade extended move: reach +/-3SD on climax volume, reversal candle,
    CVD momentum diverging."""
    cvd = np.array([b["cvd"] for b in bars])
    vols = np.array([b["volume"] for b in bars])
    sigs = []
    for i in range(6, len(bars)):
        if sd[i] <= 0:
            continue
        b = bars[i]
        upper3, lower3 = vwap[i] + 3 * sd[i], vwap[i] - 3 * sd[i]
        climax = b["volume"] >= np.quantile(vols[max(0, i - 100):i], 0.85) if i > 20 else False
        if not climax:
            continue
        # CVD divergence: price new high but cvd not confirming (for SELL)
        if b["high"] >= upper3 and b["close"] < b["open"] and cvd[i] < max(cvd[i - 5:i]):
            sigs.append((i, "SELL"))
        elif b["low"] <= lower3 and b["close"] > b["open"] and cvd[i] > min(cvd[i - 5:i]):
            sigs.append((i, "BUY"))
    return sigs


def random_sigs(bars, n, seed):
    rnd = random.Random(seed)
    idxs = sorted(rnd.sample(range(30, len(bars) - 1), min(n, len(bars) - 40)))
    return [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]


def main():
    print("Loading cached GC bars/ticks (local, no cost)...")
    bars1, ts, px = load()
    bars = resample(bars1, 5)
    vwap, sd, since = daily_vwap_bands(bars)
    atr = calc_atr(bars)
    print(f"  {len(bars):,} 5-min bars | session-anchored VWAP + 1/2/3 SD bands\n")

    strategies = {
        "1. VWAP Bounce (trend cont)":  (s1_bounce(bars, vwap, sd, since),  1.75),
        "2. VWAP Reclaim (reversal)":   (s2_reclaim(bars, vwap, sd, since), 2.5),
        "3. VWAP Deviation Scalp (MR)": (s3_deviation_scalp(bars, vwap, sd, since), 1.25),
        "4. VWAP Exhaustion (fade)":    (s4_exhaustion(bars, vwap, sd, since), 1.75),
    }

    for sname, (sigs, tgtR) in strategies.items():
        print("=" * 104)
        print(f"{sname}   ({len(sigs)} signals | target {tgtR}R)")
        print("=" * 104)
        s = stats(simulate(bars, ts, px, sigs, atr, 1.0, tgtR, 5))
        print(row(f"SL1.0xATR / TP{tgtR}xATR", s))
        if s:
            rs = stats(simulate(bars, ts, px, random_sigs(bars, len(sigs), seed=hash(sname) % 9999), atr, 1.0, tgtR, 5))
            print(row("   -> random control", rs))
        print()

    print("=" * 104)
    print("RULE: real edge only if net/trade > 0 AND clearly beats its random row.")
    print("=" * 104)


if __name__ == "__main__":
    main()
