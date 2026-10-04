"""Verification of RSI-14 Reversion + Trailing exit (the promising round-1 result):
in-sample/out-of-sample split + random-entry baseline (same trailing engine), to check this
isn't overfit/noise before considering it for live paper-trading.
Run: python backtest_rsi14_verify.py
"""
import random
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_smc_strategies_v2_full import strategy_rsi_reversion
from backtest_iteration_round1 import build_bars_full, calc_atr_series, run_trailing_exit, SPREAD, SL_ATR_MULT

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


def row(label, s):
    if s is None:
        return f"   {label:24s}: no trades"
    return f"   {label:24s}: trades {s['n']:4d} | gross ${s['gross']:8.2f} | NET ${s['net']:8.2f} | MaxDD {s['dd']:5.1f}%"


def main():
    ts, px, vol, buy = load_ticks()
    bars = build_bars_full(ts, px, vol, 60)
    atrs = calc_atr_series(bars)
    sigs = strategy_rsi_reversion(bars)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, {len(sigs)} RSI-14 signals\n")

    split_t = ts[0] + (ts[-1] - ts[0]) * 0.67
    split_idx = next(i for i, b in enumerate(bars) if ts[b["next_tick"]] >= split_t)
    print(f"Split at {datetime.fromtimestamp(split_t, timezone.utc)} (bar idx {split_idx}/{len(bars)})\n")

    full = execute(bars, ts, px, atrs, sigs)
    ins = execute(bars, ts, px, atrs, [s for s in sigs if s[0] < split_idx])
    oos = execute(bars, ts, px, atrs, [s for s in sigs if s[0] >= split_idx])
    print(row("FULL", stats(full)))
    print(row("IN-SAMPLE (67%)", stats(ins)))
    print(row("OUT-OF-SAMPLE (33%)", stats(oos)))

    print("\nRandom-entry baseline (same trailing engine, 30 seeds, same signal count):")
    full_nets, oos_nets = [], []
    for seed in range(30):
        rnd = random.Random(seed)
        idxs = sorted(rnd.sample(range(30, len(bars) - 2), min(len(sigs), len(bars) - 40)))
        rsigs = [(i, rnd.choice(["BUY", "SELL"])) for i in idxs]
        rt = execute(bars, ts, px, atrs, rsigs)
        s_full = stats(rt)
        s_oos = stats([x for x in rt if x["t"] >= split_t])
        full_nets.append(s_full["net"] if s_full else 0.0)
        oos_nets.append(s_oos["net"] if s_oos else 0.0)
    print(f"   FULL net: mean ${sum(full_nets)/len(full_nets):.2f}  range [${min(full_nets):.2f} .. ${max(full_nets):.2f}]")
    print(f"   OOS  net: mean ${sum(oos_nets)/len(oos_nets):.2f}  range [${min(oos_nets):.2f} .. ${max(oos_nets):.2f}]")


if __name__ == "__main__":
    main()
