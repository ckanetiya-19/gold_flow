"""
Standalone backtest (read-only): Port 9100 Order-Flow Zones entry logic +
Tier 1 / Tier 2 improvements discussed with the user, each tested
independently against the current baseline AND against a random-entry
control, all using the SAME tick-level SL + $2-arm/$1-trail TRAILING exit
(no fixed TP anywhere in this file, per instruction).

Everything below is evaluated CAUSALLY (bar-by-bar, only ever using bars
seen so far) - this matters a lot here because zone "mitigation" status and
HTF bias both look easy to get wrong with lookahead if computed over the
whole series at once. Swing/Order-Block zone confirmation, Daily Bias,
15m trend, and the rolling Session Value Area are all built incrementally,
the same way the live process actually sees the market.

TIERS TESTED
  BASELINE  - current live 9100 logic: swing-fractal zones (2-left/2-right
              fractal), entry on wick-into-zone + close-back-out + confirm
              candle color.
  T1_HTF    - BASELINE, but only take the trade if Daily Bias (weekly dir +
              daily dir combined, same formula as live) agrees with the
              trade direction.
  T2_OB     - zones REPLACED with Order Blocks (ICT/SMC definition): the
              last opposite-colored candle before an impulsive move of
              >= IMPULSE_MULT x ATR within LOOKAHEAD bars.
  T3_ABS    - BASELINE, but only take the trade if the signal candle shows
              volume absorption (reuses the exact same absorption formula
              already computed live: |delta|>50 and |close-open|<0.25).
  T4_RR     - BASELINE, but skip the setup unless the distance to the
              nearest opposite zone (estimated reward) is >= MIN_RR x the
              stop distance. (There's no fixed TP with a trailing exit, so
              this is used as an entry-quality filter, not an exit target -
              documented explicitly since it's an adaptation.)
  T5_CVDDIV - BASELINE, but only take the trade if Cumulative Volume Delta
              shows divergence confirming the reversal (reuses the exact
              same divergence formula already computed live).
  T6_VP     - BASELINE, but only take the trade if the zone sits near the
              rolling 6-hour session Value Area (POC/VAH/VAL) - "reinforced"
              by real volume concentration, not just price structure.
  T7_SESSION- BASELINE, but only take the trade during London+NY hours
              (13:00-22:00 UTC, the same session boundary already used
              live for the session tag).
  RANDOM_*  - random entries (same count as the tier it's paired with,
              same ATR-based stop), same trailing exit - the mandatory
              control for every tier above.
  COMBINED  - whichever individual tiers beat BOTH baseline and their own
              random control, stacked together.

DXY confluence (discussed as a possible Tier 2 idea) is DELIBERATELY NOT
included here: checked PORT_9090's code and its DXY price is a static/
hardcoded value (98.78), not a real live or historical feed - there is no
real historical DXY data anywhere in this project to honestly backtest
against, so it's left out rather than faked.

Data: real Binance PAXG ticks from QuestDB (same known caveat as every
other backtest in this project - live 9100 uses real iTick spot price;
historical iTick ticks were never stored, so PAXG is the closest real
proxy available). Spread $0.24/trade (live XAUUSD.sd spread).

Run: python backtest_orderflow_zones_tiers.py
"""
import random
from datetime import datetime, timedelta, timezone

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import build_bars as build_bars_simple, market_closed

SPREAD = 0.24
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
SL_ATR_MULT = 1.0
ZONE_ATR_BUFFER = 0.3
MAX_ZONES_KEPT = 8
IMPULSE_MULT = 1.2      # T2 order-block: forward move must be >= this x ATR
OB_LOOKAHEAD = 3        # bars after the base candle to confirm the impulse
MIN_RR = 1.5            # T4: min estimated reward:risk to accept a setup
VP_WINDOW_SECONDS = 6 * 3600
VP_BIN = 0.5
VP_TOLERANCE = 1.0      # $ distance counted as "at" the value area
VP_RECOMPUTE_EVERY = 15
SESSION_START_HOUR = 13  # UTC, London open
SESSION_END_HOUR = 22    # UTC, NY close


# ============================================================================
# Bars with real buy/sell volume + CVD + delta/absorption/div, causal by
# construction (each bar only ever sums ticks that already happened).
# ============================================================================
def build_bars_full(ts, px, vol, buy):
    bars = []
    cur = None
    cum_pv = cum_vol = cum_delta = 0.0
    for i in range(len(ts)):
        minute = int(ts[i] // 60) * 60
        p, v, b = px[i], vol[i], buy[i]
        cum_pv += p * v
        cum_vol += v
        cum_delta += v if b else -v
        vwap = cum_pv / cum_vol if cum_vol > 0 else p
        if cur is None or cur["t"] < minute:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": minute, "open": p, "high": p, "low": p, "close": p, "volume": v,
                   "buy_vol": v if b else 0.0, "sell_vol": 0.0 if b else v,
                   "vwap": vwap, "cvd": cum_delta, "last_tick": i}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
            if b:
                cur["buy_vol"] += v
            else:
                cur["sell_vol"] += v
            cur["vwap"] = vwap
            cur["cvd"] = cum_delta
            cur["last_tick"] = i
    return bars


def finalize_features(bars):
    """NOTE on absorption: the live 9080 formula (|delta|>50, body<0.25) was
    tuned for a different volume scale and turned out to almost never fire on
    PAXG bars here (checked: 95th-percentile |delta| across this whole
    dataset is ~6.4, nowhere near 50) - using it as-is made T3 statistically
    meaningless (1 trade). Replaced with a ROLLING RELATIVE definition that
    adapts to actual volume scale instead of a fixed magic number: delta in
    the top ~20% of the trailing 100-bar window, AND a small body relative
    to the bar's own range (the real "absorption" idea - big volume fight,
    small net price move)."""
    for i, b in enumerate(bars):
        delta = b["buy_vol"] - b["sell_vol"]
        b["delta"] = delta
        b["div"] = None
        if i >= 4:
            cs = b["cvd"] - bars[i - 4]["cvd"]
            ps = b["close"] - bars[i - 4]["close"]
            if cs > 0 and ps < 0:
                b["div"] = "Bullish"
            elif cs < 0 and ps > 0:
                b["div"] = "Bearish"


def compute_absorption_series(bars, window=100, delta_pctile=0.80, max_body_frac=0.5):
    """Causal, rolling-relative absorption tag: |delta| in the top
    (1-delta_pctile) of the trailing `window` bars, AND body <=
    max_body_frac of the bar's own high-low range (big fight, small net
    move)."""
    out = [None] * len(bars)
    deltas = [abs(b["buy_vol"] - b["sell_vol"]) for b in bars]
    for i, b in enumerate(bars):
        lo = max(0, i - window)
        hist = sorted(deltas[lo:i]) if i > lo else []
        if not hist:
            continue
        thresh = hist[int(len(hist) * delta_pctile)]
        rng = b["high"] - b["low"]
        body = abs(b["close"] - b["open"])
        if deltas[i] >= thresh and rng > 0 and body <= max_body_frac * rng:
            out[i] = "Bullish" if (b["buy_vol"] - b["sell_vol"]) > 0 else "Bearish"
    return out


def calc_atr_series(bars, period=14):
    atrs = [2.0] * len(bars)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        window = trs[-period:]
        atrs[i] = sum(window) / len(window)
    return atrs


# ============================================================================
# Zones - built CAUSALLY (bar-by-bar, only ever using confirmed history)
# ============================================================================
def build_swing_zones_causal(bars, atrs, left=2, right=2, max_zones=MAX_ZONES_KEPT):
    zones = []
    zones_by_bar = [[] for _ in range(len(bars))]
    for i in range(len(bars)):
        m = i - right
        if m - left >= 0:
            window = bars[m - left:i + 1]
            highs = [x["high"] for x in window]
            lows = [x["low"] for x in window]
            atr = atrs[m] if atrs[m] > 0 else 1.0
            if bars[m]["high"] == max(highs) and highs.count(bars[m]["high"]) == 1:
                zones.append({"kind": "SUPPLY", "top": bars[m]["high"] + ZONE_ATR_BUFFER * atr,
                              "bottom": min(bars[m]["open"], bars[m]["close"]), "start_idx": m, "mitigated": False})
            if bars[m]["low"] == min(lows) and lows.count(bars[m]["low"]) == 1:
                zones.append({"kind": "DEMAND", "bottom": bars[m]["low"] - ZONE_ATR_BUFFER * atr,
                              "top": max(bars[m]["open"], bars[m]["close"]), "start_idx": m, "mitigated": False})
        _mitigate(zones, bars[i]["close"])
        zones_by_bar[i] = [z for z in zones if not z["mitigated"]][-max_zones:]
    return zones_by_bar


def build_ob_zones_causal(bars, atrs, impulse_mult=IMPULSE_MULT, lookahead=OB_LOOKAHEAD, max_zones=MAX_ZONES_KEPT):
    zones = []
    zones_by_bar = [[] for _ in range(len(bars))]
    for i in range(len(bars)):
        m = i - lookahead
        if m >= 1:
            b = bars[m]
            atr = atrs[m] if atrs[m] > 0 else 1.0
            if b["close"] < b["open"]:
                fwd_high = max(bars[j]["high"] for j in range(m + 1, i + 1))
                if fwd_high - b["close"] >= impulse_mult * atr:
                    zones.append({"kind": "DEMAND", "top": max(b["open"], b["close"]),
                                  "bottom": b["low"] - ZONE_ATR_BUFFER * atr, "start_idx": m, "mitigated": False})
            if b["close"] > b["open"]:
                fwd_low = min(bars[j]["low"] for j in range(m + 1, i + 1))
                if b["close"] - fwd_low >= impulse_mult * atr:
                    zones.append({"kind": "SUPPLY", "bottom": min(b["open"], b["close"]),
                                  "top": b["high"] + ZONE_ATR_BUFFER * atr, "start_idx": m, "mitigated": False})
        _mitigate(zones, bars[i]["close"])
        zones_by_bar[i] = [z for z in zones if not z["mitigated"]][-max_zones:]
    return zones_by_bar


def _mitigate(zones, close_price):
    for z in zones:
        if z["mitigated"]:
            continue
        if z["kind"] == "SUPPLY" and close_price > z["top"]:
            z["mitigated"] = True
        if z["kind"] == "DEMAND" and close_price < z["bottom"]:
            z["mitigated"] = True


# ============================================================================
# Daily Bias (T1) - single causal pass, exact same combination rule as live
# ============================================================================
def precompute_daily_bias_series(bars):
    out = [None] * len(bars)
    cur_day = cur_week = None
    day_open = week_open = None
    for i, b in enumerate(bars):
        dt = datetime.fromtimestamp(b["t"], tz=timezone.utc)
        day = dt.date()
        week_start = day - timedelta(days=day.weekday())
        if day != cur_day:
            cur_day, day_open = day, b["open"]
        if week_start != cur_week:
            cur_week, week_open = week_start, b["open"]
        price = b["close"]
        weekly_dir = "BULLISH" if price >= week_open else "BEARISH"
        daily_dir = "BULLISH" if price >= day_open else "BEARISH"
        out[i] = weekly_dir if weekly_dir == daily_dir else "MIXED"
    return out


# ============================================================================
# Session Value Area (T6) - rolling POC/VAH/VAL from real tick volume,
# recomputed every VP_RECOMPUTE_EVERY bars using only ticks up to that bar.
# ============================================================================
def compute_session_va(ts, px, vol, upto_tick_idx, window_seconds=VP_WINDOW_SECONDS, bin_size=VP_BIN):
    end_t = ts[upto_tick_idx]
    start_t = end_t - window_seconds
    bins = {}
    j = upto_tick_idx
    while j >= 0 and ts[j] >= start_t:
        p = round(px[j] / bin_size) * bin_size
        bins[p] = bins.get(p, 0.0) + vol[j]
        j -= 1
    if not bins:
        return None
    poc = max(bins, key=bins.get)
    total = sum(bins.values())
    target = total * 0.70
    prices_sorted = sorted(bins.keys())
    lo = hi = prices_sorted.index(poc)
    acc = bins[poc]
    while acc < target and (lo > 0 or hi < len(prices_sorted) - 1):
        lo_p = prices_sorted[lo - 1] if lo > 0 else None
        hi_p = prices_sorted[hi + 1] if hi < len(prices_sorted) - 1 else None
        lo_v = bins.get(lo_p, -1) if lo_p is not None else -1
        hi_v = bins.get(hi_p, -1) if hi_p is not None else -1
        if lo_v >= hi_v and lo_p is not None:
            acc += lo_v; lo -= 1
        elif hi_p is not None:
            acc += hi_v; hi += 1
        else:
            break
    return {"poc": poc, "val": prices_sorted[lo], "vah": prices_sorted[hi]}


def precompute_va_series(bars, ts, px, vol):
    out = [None] * len(bars)
    last = None
    for i, b in enumerate(bars):
        if i % VP_RECOMPUTE_EVERY == 0 or last is None:
            last = compute_session_va(ts, px, vol, b["last_tick"])
        out[i] = last
    return out


def vp_reinforces(kind, zone_top, zone_bottom, va, tol=VP_TOLERANCE):
    if va is None:
        return False
    if kind == "DEMAND":
        return abs(zone_top - va["val"]) <= tol or abs(zone_top - va["poc"]) <= tol
    return abs(zone_bottom - va["vah"]) <= tol or abs(zone_bottom - va["poc"]) <= tol


def in_liquid_session(t):
    hr = datetime.fromtimestamp(t, tz=timezone.utc).hour
    return SESSION_START_HOUR <= hr < SESSION_END_HOUR


# ============================================================================
# Signal generation - one function, every tier is just a combination of
# switches, so BASELINE and COMBINED share the exact same code path.
# ============================================================================
def generate_signals(bars, atrs, zones_by_bar, daily_bias=None, va_series=None, absorption=None,
                      require_htf=False, require_abs=False, require_div=False,
                      require_vp=False, require_session=False, min_rr=None):
    sigs = []
    for i in range(6, len(bars)):
        b = bars[i]
        zones = zones_by_bar[i - 1]
        atr = atrs[i] if atrs[i] > 0 else 1.0

        if require_session and not in_liquid_session(b["t"]):
            continue

        for z in zones:
            d = None
            if z["kind"] == "DEMAND" and b["low"] <= z["top"] and b["close"] > z["top"] and b["close"] > b["open"]:
                d = "BUY"
                entry_est, sl = z["top"], z["bottom"] - ZONE_ATR_BUFFER * atr
            elif z["kind"] == "SUPPLY" and b["high"] >= z["bottom"] and b["close"] < z["bottom"] and b["close"] < b["open"]:
                d = "SELL"
                entry_est, sl = z["bottom"], z["top"] + ZONE_ATR_BUFFER * atr
            if d is None:
                continue

            if require_htf and daily_bias is not None:
                want = "BULLISH" if d == "BUY" else "BEARISH"
                if daily_bias[i] != want:
                    continue
            if require_abs:
                want = "Bullish" if d == "BUY" else "Bearish"
                if absorption is None or absorption[i] != want:
                    continue
            if require_div:
                want = "Bullish" if d == "BUY" else "Bearish"
                if b.get("div") != want:
                    continue
            if require_vp and va_series is not None:
                if not vp_reinforces(z["kind"], z["top"], z["bottom"], va_series[i]):
                    continue
            if min_rr is not None:
                risk = entry_est - sl if d == "BUY" else sl - entry_est
                if risk <= 0:
                    continue
                if d == "BUY":
                    opp = [zz for zz in zones if zz["kind"] == "SUPPLY" and zz["bottom"] > entry_est]
                    reward = (min(zz["bottom"] for zz in opp) - entry_est) if opp else 2 * risk
                else:
                    opp = [zz for zz in zones if zz["kind"] == "DEMAND" and zz["top"] < entry_est]
                    reward = (entry_est - max(zz["top"] for zz in opp)) if opp else 2 * risk
                if reward / risk < min_rr:
                    continue

            sigs.append((i, d, sl))
            break  # one signal per bar, first matching zone (same as live)
    return sigs


def generate_random_signals(bars, atrs, n, seed=7):
    rnd = random.Random(seed)
    pool = list(range(6, len(bars) - 1))
    idxs = sorted(rnd.sample(pool, min(n, len(pool))))
    sigs = []
    for i in idxs:
        d = rnd.choice(["BUY", "SELL"])
        atr = atrs[i] if atrs[i] > 0 else 1.0
        entry_est = bars[i]["close"]
        sl = entry_est - SL_ATR_MULT * atr if d == "BUY" else entry_est + SL_ATR_MULT * atr
        sigs.append((i, d, sl))
    return sigs


# ============================================================================
# Execution - tick-level SL + $2-arm/$1-trail trailing exit (no fixed TP)
# ============================================================================
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


def execute(bars, ts, px, sigs):
    trades = []
    free_idx = -1
    for i, d, sl in sorted(sigs, key=lambda s: s[0]):
        b = bars[i]
        nxt = b["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        if d == "BUY" and entry <= sl:
            continue
        if d == "SELL" and entry >= sl:
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
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, w=w, wr=w / n * 100, gross=gross, net=net, avg=net / n, dd=dd)


def row(label, s):
    if s is None:
        return f"   {label:34s}: no trades"
    return (f"   {label:34s}: trades {s['n']:4d} | WR {s['wr']:5.1f}% | gross ${s['gross']:8.2f} | "
            f"NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


# ============================================================================
def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days of real PAXG ticks | spread ${SPREAD} | "
          f"trail arm ${TRAIL_ARM}/dist ${TRAIL_DIST} | zone-ATR-buffer {ZONE_ATR_BUFFER}x\n")

    bars = build_bars_full(ts, px, vol, buy)
    finalize_features(bars)
    atrs = calc_atr_series(bars)
    print(f"{len(bars)} one-minute bars built.\n")

    print("Building zones (causal)...")
    swing_zones = build_swing_zones_causal(bars, atrs)
    ob_zones = build_ob_zones_causal(bars, atrs)
    print("Building Daily Bias series (causal)...")
    daily_bias = precompute_daily_bias_series(bars)
    print("Building rolling 6h Session Value Area (causal)...")
    va_series = precompute_va_series(bars, ts, px, vol)
    print("Building rolling-relative absorption series (causal)...\n")
    absorption = compute_absorption_series(bars)

    variants = {
        "BASELINE (swing zones)": generate_signals(bars, atrs, swing_zones),
        "T1_HTF (+ Daily Bias filter)": generate_signals(bars, atrs, swing_zones, daily_bias=daily_bias, require_htf=True),
        "T2_OB (Order Block zones)": generate_signals(bars, atrs, ob_zones),
        "T3_ABS (+ volume absorption)": generate_signals(bars, atrs, swing_zones, absorption=absorption, require_abs=True),
        "T4_RR (+ min 1.5R estimated)": generate_signals(bars, atrs, swing_zones, min_rr=MIN_RR),
        "T5_CVDDIV (+ CVD divergence)": generate_signals(bars, atrs, swing_zones, require_div=True),
        "T6_VP (+ Value Area reinforce)": generate_signals(bars, atrs, swing_zones, va_series=va_series, require_vp=True),
        "T7_SESSION (London+NY only)": generate_signals(bars, atrs, swing_zones, require_session=True),
    }

    print("=" * 120)
    print("INDIVIDUAL TIERS vs RANDOM CONTROL (same signal count, same trailing exit)")
    print("=" * 120)
    results = {}
    for seed_i, (name, sigs) in enumerate(variants.items()):
        tr = execute(bars, ts, px, sigs)
        s = stats(tr)
        results[name] = s
        print(row(name, s))
        rnd_sigs = generate_random_signals(bars, atrs, len(sigs), seed=1000 + seed_i)
        rnd_tr = execute(bars, ts, px, rnd_sigs)
        rs = stats(rnd_tr)
        print(row(f"   -> random control ({len(sigs)} sigs)", rs))
        print()

    print("=" * 120)
    print("Now check output above: a tier only counts as a real improvement if its")
    print("net/trade beats BOTH the BASELINE row and its own random-control row.")
    print("=" * 120)
    print()

    print("=" * 120)
    print("COMBINED variants - stacking whichever individual tiers looked genuinely additive")
    print("=" * 120)
    combined = {
        "COMBINED: OB zones + min-RR": generate_signals(bars, atrs, ob_zones, min_rr=MIN_RR),
        "COMBINED: OB zones + min-RR + CVDdiv": generate_signals(bars, atrs, ob_zones, min_rr=MIN_RR, require_div=True),
        "COMBINED: OB zones + min-RR + absorption": generate_signals(bars, atrs, ob_zones, min_rr=MIN_RR, absorption=absorption, require_abs=True),
        "COMBINED: OB zones + min-RR + HTF": generate_signals(bars, atrs, ob_zones, min_rr=MIN_RR, daily_bias=daily_bias, require_htf=True),
    }
    for seed_i, (name, sigs) in enumerate(combined.items()):
        tr = execute(bars, ts, px, sigs)
        s = stats(tr)
        print(row(name, s))
        rnd_sigs = generate_random_signals(bars, atrs, len(sigs), seed=2000 + seed_i)
        rnd_tr = execute(bars, ts, px, rnd_sigs)
        rs = stats(rnd_tr)
        print(row(f"   -> random control ({len(sigs)} sigs)", rs))
        print()

    print("=" * 120)
    print("IN-SAMPLE (first 67%) / OUT-OF-SAMPLE (last 33%) CHECK - the two most")
    print("robust individual tiers, and the best combined variant")
    print("=" * 120)
    split_i = int(len(bars) * 0.67)
    split_t = bars[split_i]["t"]

    def split_trades(tr):
        return [x for x in tr if x["t"] < split_t], [x for x in tr if x["t"] >= split_t]

    for name, sigs in {
        "BASELINE (swing zones)": variants["BASELINE (swing zones)"],
        "T2_OB (Order Block zones)": variants["T2_OB (Order Block zones)"],
        "T4_RR (+ min 1.5R estimated)": variants["T4_RR (+ min 1.5R estimated)"],
        "COMBINED: OB zones + min-RR": combined["COMBINED: OB zones + min-RR"],
        "COMBINED: OB zones + min-RR + CVDdiv": combined["COMBINED: OB zones + min-RR + CVDdiv"],
    }.items():
        tr = execute(bars, ts, px, sigs)
        is_tr, oos_tr = split_trades(tr)
        print(f"{name}:")
        print(row("  in-sample (first 67%)", stats(is_tr)))
        print(row("  out-of-sample (last 33%)", stats(oos_tr)))
        print()


if __name__ == "__main__":
    main()
