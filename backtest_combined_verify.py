"""Combine RSI-14 Reversion + Volume Imbalance Spike + ADX-VWAP-Fade into one logic (same
Trailing exit as before: $2 arm / $1 trail, SL=1xATR), tested two ways:
  PORTFOLIO  = any of the 3 fires independently (union of all signals)
  CONFLUENCE = only when >=2 of the 3 agree on direction within a 5-bar window
Both verified against a random-entry baseline (same engine, same signal count), FULL period.
Run: python backtest_combined_verify.py
"""
import random

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_smc_strategies_v2_full import strategy_rsi_reversion, strategy_volume_imbalance
from backtest_iteration_round1 import (
    build_bars_full, calc_atr_series, run_trailing_exit, strategy_adx_vwap_fade, SPREAD, SL_ATR_MULT,
)

WINDOW = 5


def execute(bars, ts, px, atrs, signals):
    trades, free_idx = [], -1
    for i, d in signals:
        if i >= len(bars):
            continue
        nxt = bars[i]["next_tick"]
        if nxt <= free_idx or market_closed(ts[nxt]):
            continue
        entry = px[nxt]
        atr = atrs[i]
        sl = entry - SL_ATR_MULT * atr if d == "BUY" else entry + SL_ATR_MULT * atr
        xp, xi, why = run_trailing_exit(ts, px, nxt, d, entry, sl)
        if why == "OPEN":
            continue
        trades.append({"t": ts[nxt], "d": d, "pnl": pnl_of(d, entry, xp), "why": why})
        free_idx = xi
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return None
    gross = sum(x["pnl"] for x in tr)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(tr, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    return dict(n=n, gross=gross, net=net, dd=dd)


def confluence(sig_lists, min_agree, window=WINDOW, cooldown=5):
    tagged = []
    for k, sigs in enumerate(sig_lists):
        for idx, d in sigs:
            tagged.append((idx, d, k))
    tagged.sort()
    out = []
    last_fire = -999
    for idx, d, k in tagged:
        if idx - last_fire < cooldown:
            continue
        agree = {k}
        for idx2, d2, k2 in tagged:
            if idx - window <= idx2 <= idx and d2 == d:
                agree.add(k2)
        if len(agree) >= min_agree:
            out.append((idx, d)); last_fire = idx
    return out


def verify(label, bars, ts, px, atrs, signals):
    tr = execute(bars, ts, px, atrs, signals)
    s = stats(tr)
    print(f"\n{label}  ({len(signals)} signals)")
    if s is None:
        print("   no trades")
        return
    print(f"   ACTUAL      : trades {s['n']:4d} | gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | MaxDD {s['dd']:5.1f}%")
    nets = []
    for seed in range(30):
        rnd = random.Random(seed)
        idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(len(signals), len(bars) - 40))) if signals else []
        rsigs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
        rs = stats(execute(bars, ts, px, atrs, rsigs))
        nets.append(rs["net"] if rs else 0.0)
    if not nets:
        return
    mean_net = sum(nets) / len(nets)
    beat = sum(1 for x in nets if s['net'] > x)
    print(f"   RANDOM (30x): mean net ${mean_net:8.2f} | range [${min(nets):.2f} .. ${max(nets):.2f}]")
    print(f"   Actual beat {beat}/30 random seeds. Edge over random mean: ${s['net'] - mean_net:.2f}")


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars_full(ts, px, vol, 60)
    atrs = calc_atr_series(bars)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, spread ${SPREAD}, FULL period only\n")

    rsi_sigs = strategy_rsi_reversion(bars)
    vi_sigs = strategy_volume_imbalance(bars)
    adx_sigs = strategy_adx_vwap_fade(bars)
    print(f"RSI-14: {len(rsi_sigs)} signals | Volume Imbalance: {len(vi_sigs)} | ADX-Fade: {len(adx_sigs)}")

    portfolio = sorted(rsi_sigs + vi_sigs + adx_sigs)
    verify("PORTFOLIO (all 3, independent, union)", bars, ts, px, atrs, portfolio)

    conf2 = confluence([rsi_sigs, vi_sigs, adx_sigs], min_agree=2)
    verify("CONFLUENCE 2-of-3 agree", bars, ts, px, atrs, conf2)

    conf3 = confluence([rsi_sigs, vi_sigs, adx_sigs], min_agree=3)
    verify("CONFLUENCE 3-of-3 agree", bars, ts, px, atrs, conf3)


if __name__ == "__main__":
    main()
