"""
Standalone backtest (read-only): "VWAP Full-Crossover" strategy, exactly as
specified by the user from two annotated chart screenshots (100.png/101.png).

RULE (confirmed with the user before writing this):
  - Track the SAME rolling chart VWAP already shown on 9080/9060/9100
    (TP-weighted, rolling ~100-bar window - matches the "Upper Band
    (+1.28sigma)" line visible in the screenshots, not a cumulative-since-
    session VWAP).
  - A bar is a valid BUY signal if its ENTIRE range (low AND close) is above
    the live VWAP value AND it closed green, AND the previous bar was NOT
    already fully above VWAP (this is the "first time" crossover - a bar
    whose wick pokes back below VWAP, even if its close/body is above,
    invalidates it and does not reset/consume the signal - confirmed via
    the white-circled counter-example in 100.png).
  - SELL is the exact mirror (fully below VWAP, red, previous bar not
    already fully below).
  - Entry: instant market order on the very next bar after the signal bar.
  - SL: the VWAP price AT THE SIGNAL BAR, minus $1 (BUY) / plus $1 (SELL) -
    not ATR-based, not entry-based.
  - TP: 2x the resulting risk (entry-to-SL distance) - a 2R cap.
  - Exit: SL trails once armed ($2 arm / $1 trail, the same convention used
    everywhere else in this project), OR the 2R TP is hit first - whichever
    comes first.

Per explicit instruction: FULL PERIOD ONLY, no in-sample/out-of-sample
split. Mandatory random-entry control with the identical exit mechanism,
same rigor as every other backtest in this project.

Data: real Binance PAXG ticks from QuestDB (documented project-wide caveat:
historical iTick spot was never stored, PAXG is the closest real proxy).
Spread $0.24/trade (live XAUUSD.sd spread).

Run: python backtest_vwap_full_crossover.py
"""
import random

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_orderflow_zones_tiers import build_bars_full  # per-bar volume + next_tick (build_bars in
# backtest_vwap_first_close has NO per-bar "volume" field at all - it only tracks a cumulative
# session VWAP internally - using it here silently made rolling_vwap_series fall back to
# vwap=close for every single bar (found via debug: 0/13186 bars were ever ABOVE/BELOW, all
# STRADDLE). build_bars_full has real per-bar volume, which the TP-weighted rolling formula needs.

SPREAD = 0.24
SL_BUFFER = 1.0          # $1 buffer from the VWAP price, per the confirmed rule
TP_R_MULT = 2.0          # TP = 2x the risk (2R cap)
TRAIL_ARM = 2.0          # same convention as the rest of this project
TRAIL_DIST = 1.0
VWAP_WINDOW = 100        # matches _rolling_chart_vwap on 9080/9100


def rolling_vwap_series(bars, window=VWAP_WINDOW):
    """Same formula as _rolling_chart_vwap / rolling_chart_vwap elsewhere in
    this project: TP-weighted, rolling window (not cumulative-since-start)."""
    out = [None] * len(bars)
    for i in range(len(bars)):
        wstart = max(0, i - window + 1)
        cum_pv = cum_vol = 0.0
        for b in bars[wstart:i + 1]:
            tp = (b["high"] + b["low"] + b["close"]) / 3.0
            vol = b.get("volume", 0) or 0
            cum_pv += tp * vol
            cum_vol += vol
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else bars[i]["close"]
    return out


def bar_state(bar, vwap):
    if bar["low"] > vwap:
        return "ABOVE"
    if bar["high"] < vwap:
        return "BELOW"
    return "STRADDLE"


def generate_signals(bars, vwaps):
    sigs = []
    prev_state = None
    for i in range(1, len(bars)):
        b = bars[i]
        state = bar_state(b, vwaps[i])
        if state == "ABOVE" and b["close"] > b["open"] and prev_state != "ABOVE":
            sl = round(vwaps[i] - SL_BUFFER, 2)
            sigs.append((i, "BUY", sl))
        elif state == "BELOW" and b["close"] < b["open"] and prev_state != "BELOW":
            sl = round(vwaps[i] + SL_BUFFER, 2)
            sigs.append((i, "SELL", sl))
        prev_state = state
    return sigs


def generate_random_signals(bars, n, seed=42):
    rnd = random.Random(seed)
    idxs = sorted(rnd.sample(range(1, len(bars) - 1), min(n, len(bars) - 2)))
    sigs = []
    for i in idxs:
        d = rnd.choice(["BUY", "SELL"])
        sl = round(bars[i]["close"] - SL_BUFFER, 2) if d == "BUY" else round(bars[i]["close"] + SL_BUFFER, 2)
        sigs.append((i, d, sl))
    return sigs


def run_trailing_with_tp(ts, px, start, d, entry, sl0, tp):
    long_ = d == "BUY"
    best = entry
    sl = sl0
    armed = False
    for k in range(start, len(ts)):
        p = px[k]
        if long_:
            if p >= tp:
                return tp, k, "TP"
            best = max(best, p)
            if not armed and (best - entry) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = max(sl, best - TRAIL_DIST)
            if p <= sl:
                return sl, k, ("TRAIL" if armed else "SL")
        else:
            if p <= tp:
                return tp, k, "TP"
            best = min(best, p)
            if not armed and (entry - best) >= TRAIL_ARM:
                armed = True
            if armed:
                sl = min(sl, best + TRAIL_DIST)
            if p >= sl:
                return sl, k, ("TRAIL" if armed else "SL")
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
        risk = abs(entry - sl)
        tp = round(entry + TP_R_MULT * risk, 2) if d == "BUY" else round(entry - TP_R_MULT * risk, 2)
        xp, xi, why = run_trailing_with_tp(ts, px, nxt, d, entry, sl, tp)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "risk": risk, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    wins = sum(1 for x in tr if x["why"] in ("TP", "TRAIL"))
    n_tp = sum(1 for x in tr if x["why"] == "TP")
    n_trail = sum(1 for x in tr if x["why"] == "TRAIL")
    n_sl = sum(1 for x in tr if x["why"] == "SL")
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, wr=wins / n * 100, n_tp=n_tp, n_trail=n_trail, n_sl=n_sl,
                gross=gross, net=net, avg=net / n, dd=dd)


def row(label, s):
    if s is None:
        return f"   {label:32s}: no trades"
    return (f"   {label:32s}: trades {s['n']:4d} | WR {s['wr']:5.1f}% "
            f"(TP:{s['n_tp']} TRAIL:{s['n_trail']} SL:{s['n_sl']}) | "
            f"gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | net/trade ${s['avg']:6.3f} | MaxDD {s['dd']:5.1f}%")


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days of real PAXG ticks | spread ${SPREAD} | "
          f"SL=VWAP+/-${SL_BUFFER} | TP={TP_R_MULT}R | trail arm ${TRAIL_ARM}/dist ${TRAIL_DIST}\n")

    bars = build_bars_full(ts, px, vol, buy)
    print(f"{len(bars)} one-minute bars built.")
    vwaps = rolling_vwap_series(bars)
    print("Rolling chart VWAP computed (matches 9080/9100's own VWAP line).\n")

    sigs = generate_signals(bars, vwaps)
    print(f"{len(sigs)} full-crossover signals found "
          f"(BUY: {sum(1 for s in sigs if s[1]=='BUY')}, SELL: {sum(1 for s in sigs if s[1]=='SELL')})\n")

    print("=" * 120)
    tr = execute(bars, ts, px, sigs)
    s = stats(tr)
    print(row("VWAP FULL-CROSSOVER (this strategy)", s))
    rnd_sigs = generate_random_signals(bars, len(sigs), seed=99)
    rnd_tr = execute(bars, ts, px, rnd_sigs)
    print(row(f"   -> random control ({len(sigs)} sigs)", stats(rnd_tr)))
    print("=" * 120)

    print("\nBUY-only / SELL-only breakdown:")
    for side in ("BUY", "SELL"):
        side_tr = execute(bars, ts, px, [s for s in sigs if s[1] == side])
        print(row(f"{side} only", stats(side_tr)))


if __name__ == "__main__":
    main()
