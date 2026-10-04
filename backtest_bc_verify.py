"""Verify B (Volume Imbalance + Trailing) and C (ADX-VWAP-Fade + Trailing) against a random-entry
baseline (same trailing exit engine, same signal count), FULL period only.
Run: python backtest_bc_verify.py
"""
import random

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_smc_strategies_v2_full import strategy_volume_imbalance
from backtest_iteration_round1 import (
    build_bars_full, calc_atr_series, run_trailing_exit, strategy_adx_vwap_fade, SPREAD, SL_ATR_MULT,
)


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


def verify(label, bars, ts, px, atrs, signals):
    tr = execute(bars, ts, px, atrs, signals)
    s = stats(tr)
    print(f"\n{label}")
    if s is None:
        print("   no trades")
        return
    print(f"   ACTUAL      : trades {s['n']:4d} | gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | MaxDD {s['dd']:5.1f}%")

    nets, dds = [], []
    for seed in range(30):
        rnd = random.Random(seed)
        idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(len(signals), len(bars) - 40)))
        rsigs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
        rs = stats(execute(bars, ts, px, atrs, rsigs))
        nets.append(rs["net"] if rs else 0.0)
        dds.append(rs["dd"] if rs else 0.0)
    mean_net = sum(nets) / len(nets)
    print(f"   RANDOM (30x): mean net ${mean_net:8.2f} | range [${min(nets):.2f} .. ${max(nets):.2f}] | mean MaxDD {sum(dds)/len(dds):.1f}%")
    beat = sum(1 for x in nets if s['net'] > x)
    print(f"   Actual beat {beat}/30 random seeds. Edge over random mean: ${s['net'] - mean_net:.2f}")


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars_full(ts, px, vol, 60)
    atrs = calc_atr_series(bars)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, spread ${SPREAD}, FULL period only\n")

    vi_sigs = strategy_volume_imbalance(bars)
    verify("B) Volume Imbalance Spike + Trailing", bars, ts, px, atrs, vi_sigs)

    adx_sigs = strategy_adx_vwap_fade(bars)
    verify("C) ADX-conditioned VWAP Fade + Trailing", bars, ts, px, atrs, adx_sigs)


if __name__ == "__main__":
    main()
