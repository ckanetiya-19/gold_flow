"""
LOCAL ONLY (no API, no cost): strategy SEARCH on REAL CME gold-futures
order-flow (cached gc_bars.pkl / gc_ticks.npz). Designs and tests several
strategies that actually USE the real per-trade aggressor data (real CVD,
delta, absorption) - the whole reason we bought this data - each against a
random-entry control with the identical exit engine. Same rigor bar as the
rest of this project: a strategy only "counts" if it beats BOTH break-even
AND its own random control.

Exits use ATR-scaled SL/TP (GC moves in bigger dollar terms than spot, so
fixed $2 scalps were unsuitable) plus a time-stop. GC real spread $0.31.
PnL in price points ($/$1 move).

Run: python backtest_gc_strategy_search.py
"""
import pickle
import numpy as np
import random

GC_SPREAD = 0.31
ATR_PERIOD = 14
TIME_STOP_BARS = 120          # close if neither SL nor TP hit within this many minutes
random.seed(12345)


def load():
    with open("gc_bars.pkl", "rb") as f:
        bars = pickle.load(f)
    z = np.load("gc_ticks.npz")
    return bars, z["ts"], z["px"]


def calc_atr(bars, period=ATR_PERIOD):
    atr = [2.0] * len(bars)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr[i] = sum(trs[-period:]) / len(trs[-period:])
    return atr


def market_closed(t):
    import datetime
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    wd, hr = d.weekday(), d.hour
    return (wd == 4 and hr >= 21) or wd == 5 or (wd == 6 and hr < 22)


# ---------------------------------------------------------------- exit engine
def simulate(bars, ts, px, sigs, atr, sl_mult, tp_mult, spread=GC_SPREAD):
    """Tick-level fixed SL/TP (ATR-scaled) + time stop. One position at a time."""
    trades = []
    free_tick = -1
    n = len(px)
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
        stop_ts = ts[nxt] + TIME_STOP_BARS * 60
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
    eq = peak = 0.0
    dd = 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dict(n=n, wr=wins / n * 100, net=net, avg=net / n, dd=dd)


def row(label, s):
    if s is None:
        return f"   {label:38s}: no trades"
    return (f"   {label:38s}: trades {s['n']:5d} | WR {s['wr']:5.1f}% | "
            f"NET ${s['net']:9.1f} | net/trade ${s['avg']:6.3f} | MaxDD(pts) {s['dd']:7.1f}")


# ---------------------------------------------------------------- indicators
def cvd_series(bars):
    return np.array([b["cvd"] for b in bars])


def vwap_series(bars):
    return np.array([b["vwap"] for b in bars])


# ---------------------------------------------------------------- strategies (order-flow driven)
def sig_cvd_divergence(bars, cvd, look=10):
    """Mean-reversion: price new N-bar low but CVD higher low -> BUY (buyers
    absorbing the down move). Mirror for SELL. Needs REAL cvd."""
    sigs = []
    for i in range(look + 1, len(bars)):
        lows = [bars[j]["low"] for j in range(i - look, i)]
        highs = [bars[j]["high"] for j in range(i - look, i)]
        if bars[i]["low"] < min(lows) and cvd[i] > min(cvd[i - look:i]):
            sigs.append((i, "BUY"))
        elif bars[i]["high"] > max(highs) and cvd[i] < max(cvd[i - look:i]):
            sigs.append((i, "SELL"))
    return sigs


def sig_cvd_trend(bars, cvd, vwap, look=20):
    """Continuation: CVD breaks N-bar high AND price above VWAP -> BUY."""
    sigs = []
    for i in range(look + 1, len(bars)):
        if cvd[i] > max(cvd[i - look:i]) and bars[i]["close"] > vwap[i]:
            sigs.append((i, "BUY"))
        elif cvd[i] < min(cvd[i - look:i]) and bars[i]["close"] < vwap[i]:
            sigs.append((i, "SELL"))
    return sigs


def sig_delta_exhaustion(bars, look=20, pct=0.9):
    """Reversal: extreme same-direction delta at an N-bar price extreme =
    aggressor exhaustion -> fade. Uses REAL delta."""
    deltas = np.array([abs(b["delta"]) for b in bars])
    sigs = []
    for i in range(look + 1, len(bars)):
        thresh = np.quantile(deltas[max(0, i - 100):i], pct) if i > 100 else deltas[:i].max() if i > 0 else 0
        d = bars[i]["delta"]
        if abs(d) < thresh:
            continue
        highs = [bars[j]["high"] for j in range(i - look, i)]
        lows = [bars[j]["low"] for j in range(i - look, i)]
        if d > 0 and bars[i]["high"] > max(highs):
            sigs.append((i, "SELL"))   # buyers exhausted at the top
        elif d < 0 and bars[i]["low"] < min(lows):
            sigs.append((i, "BUY"))    # sellers exhausted at the bottom
    return sigs


def sig_delta_momentum(bars, look=100, pct=0.8):
    """Trend-follow: strong delta agreeing with the bar's own direction -> follow."""
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


def sig_absorption(bars, look=100, vol_pct=0.85):
    """Big volume + small range = absorption; fade the extreme."""
    vols = np.array([b["volume"] for b in bars])
    sigs = []
    for i in range(look + 1, len(bars)):
        vthresh = np.quantile(vols[i - look:i], vol_pct)
        rng = bars[i]["high"] - bars[i]["low"]
        avg_rng = np.mean([bars[j]["high"] - bars[j]["low"] for j in range(i - look, i)])
        if bars[i]["volume"] >= vthresh and rng < 0.6 * avg_rng:
            # fade toward the mean: if bar closed up on absorption -> sellers absorbed -> SELL
            if bars[i]["delta"] > 0:
                sigs.append((i, "SELL"))
            elif bars[i]["delta"] < 0:
                sigs.append((i, "BUY"))
    return sigs


def random_sigs(bars, n, seed):
    rnd = random.Random(seed)
    idxs = sorted(rnd.sample(range(30, len(bars) - 1), min(n, len(bars) - 40)))
    return [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]


def main():
    print("Loading cached GC bars/ticks (local, no cost)...")
    bars, ts, px = load()
    print(f"  {len(bars):,} bars | {len(px):,} ticks")
    atr = calc_atr(bars)
    cvd = cvd_series(bars)
    vwap = vwap_series(bars)

    strategies = {
        "CVD Divergence (mean-rev)": sig_cvd_divergence(bars, cvd),
        "CVD Trend (continuation)": sig_cvd_trend(bars, cvd, vwap),
        "Delta Exhaustion (fade)": sig_delta_exhaustion(bars),
        "Delta Momentum (follow)": sig_delta_momentum(bars),
        "Absorption (fade)": sig_absorption(bars),
    }

    # test each with a couple of ATR exit profiles
    exit_profiles = [(1.0, 2.0), (1.0, 1.5), (1.5, 3.0)]

    for sname, sigs in strategies.items():
        print("\n" + "=" * 108)
        print(f"{sname}  ({len(sigs)} raw signals)")
        print("=" * 108)
        for sl_m, tp_m in exit_profiles:
            tr = simulate(bars, ts, px, sigs, atr, sl_m, tp_m)
            s = stats(tr)
            print(row(f"SL{sl_m}xATR/TP{tp_m}xATR", s))
            if s:
                rnd = random_sigs(bars, len(sigs), seed=int(sl_m * 100 + tp_m * 7) + hash(sname) % 1000)
                rs = stats(simulate(bars, ts, px, rnd, atr, sl_m, tp_m))
                print(row(f"   -> random ({rs['n'] if rs else 0} tr)", rs))

    print("\n" + "=" * 108)
    print("RULE: a strategy is only real if net/trade > 0 AND clearly beats its random row.")
    print("=" * 108)


if __name__ == "__main__":
    main()
