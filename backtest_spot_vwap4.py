"""
LOCAL ONLY (no cost): backtest the 4 VWAP strategies from the slides on our
LIVE SPOT data - the real QuestDB ticks (ticks_v2) that the live 9060/9080/
9100 system actually uses. GC futures ignored per user request.

BACKTEST-ONLY ADAPTATIONS (do NOT touch live code): the slides use a
session/daily-anchored VWAP + 2SD/3SD bands, which live doesn't have. Here
VWAP is re-anchored each UTC day with 1/2/3-SD volume-weighted bands, on
5-minute bars (the slides are 5-min). Exits normalized to ATR so each
strategy is directly comparable to a random-entry control (isolates whether
the ENTRY has edge). Spot spread $0.24. PnL in price points.

Run: python backtest_spot_vwap4.py
"""
import numpy as np
import random
import datetime

from backtest_9080_all_strategies_oos import load_ticks
from backtest_orderflow_zones_tiers import build_bars_full

SPOT_SPREAD = 0.24
ATR_PERIOD = 14
TIME_STOP_BARS = 48
random.seed(77)


def resample(bars, tf_min):
    out, cur = [], None
    for b in bars:
        bucket = int(b["t"] // (tf_min * 60)) * (tf_min * 60)
        if cur is None or cur["t"] != bucket:
            if cur is not None:
                out.append(cur)
            cur = {"t": bucket, "open": b["open"], "high": b["high"], "low": b["low"],
                   "close": b["close"], "volume": b.get("volume", 0), "buy_vol": b.get("buy_vol", 0),
                   "sell_vol": b.get("sell_vol", 0), "cvd": b.get("cvd", 0), "next_tick": b["next_tick"]}
        else:
            cur["high"] = max(cur["high"], b["high"]); cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]; cur["volume"] += b.get("volume", 0)
            cur["buy_vol"] += b.get("buy_vol", 0); cur["sell_vol"] += b.get("sell_vol", 0)
            cur["cvd"] = b.get("cvd", 0); cur["next_tick"] = b["next_tick"]
    if cur is not None:
        out.append(cur)
    for b in out:
        b["delta"] = b["buy_vol"] - b["sell_vol"]
    return out


def daily_vwap_bands(bars):
    vwap = np.zeros(len(bars)); sd = np.zeros(len(bars)); since = np.zeros(len(bars), dtype=int)
    cur_day = None; cum_pv = cum_v = cum_pv2 = 0.0; n = 0
    for i, b in enumerate(bars):
        day = datetime.datetime.fromtimestamp(b["t"], datetime.timezone.utc).date()
        if day != cur_day:
            cur_day = day; cum_pv = cum_v = cum_pv2 = 0.0; n = 0
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b["volume"] or 1e-9
        cum_pv += tp * v; cum_v += v; cum_pv2 += tp * tp * v
        vw = cum_pv / cum_v
        vwap[i] = vw; sd[i] = max(0.0, cum_pv2 / cum_v - vw * vw) ** 0.5; since[i] = n
        n += 1
    return vwap, sd, since


def calc_atr(bars, period=ATR_PERIOD):
    atr = [1.0] * len(bars); trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr[i] = sum(trs[-period:]) / len(trs[-period:])
    return atr


def market_closed(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    wd, hr = d.weekday(), d.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


def simulate(bars, ts, px, sigs, atr, sl_mult, tp_mult, tf_min, spread=SPOT_SPREAD):
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


def s1_bounce(bars, vwap, sd, since):
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
    cvd = np.array([b["cvd"] for b in bars]); vols = np.array([b["volume"] for b in bars])
    sigs = []
    for i in range(6, len(bars)):
        if sd[i] <= 0:
            continue
        b = bars[i]
        upper3, lower3 = vwap[i] + 3 * sd[i], vwap[i] - 3 * sd[i]
        climax = b["volume"] >= np.quantile(vols[max(0, i - 100):i], 0.85) if i > 20 else False
        if not climax:
            continue
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
    ts, px, vol, buy = load_ticks()
    bars1 = build_bars_full(ts, px, vol, buy)
    bars = resample(bars1, 5)
    vwap, sd, since = daily_vwap_bands(bars)
    atr = calc_atr(bars)
    print(f"LIVE SPOT data | {(ts[-1]-ts[0])/86400:.1f} days | {len(bars):,} 5-min bars | "
          f"session VWAP + 1/2/3 SD | spread ${SPOT_SPREAD}\n")

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
