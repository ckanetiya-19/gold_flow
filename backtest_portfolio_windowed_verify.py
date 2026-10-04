"""Re-verify the 4-strategy portfolio using the SAME 300-bar-windowed VWAP/ADX computation the
LIVE module (portfolio_strategies.py) actually uses (historical_bars is capped at 200-300 bars
in the live ports, unlike the original backtest's unbounded-cumulative VWAP). This validates what
will actually run live, not a subtly different unbounded version.
Run: python backtest_portfolio_windowed_verify.py
"""
import random
from datetime import datetime, timezone

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_iteration_round1 import build_bars_full, calc_atr_series, run_trailing_exit, SPREAD, SL_ATR_MULT
from backtest_adx_type1_vs_type2 import build_bars_with_flow
import portfolio_strategies as ps

WINDOW = 300


def make_all_signals(bars):
    """Replays CHECKS exactly as the live module would, but scanning historical data with the
    same 300-bar trailing window cap, and the same per-strategy cooldown."""
    out = {k: [] for k in ps.CHECKS}
    cooldown_until = {k: -1 for k in ps.CHECKS}
    n = len(bars)
    for i in range(60, n):  # need enough bars for ADX/RSI/VolAvg to be meaningful
        window = bars[max(0, i - WINDOW + 1):i + 1]
        for name, check_fn in ps.CHECKS.items():
            if i <= cooldown_until[name]:
                continue
            try:
                d = check_fn(window)
            except Exception:
                d = None
            if d:
                out[name].append((i, d))
                cooldown_until[name] = i + ps.COOLDOWN_BARS
    return out


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
    bars = build_bars_with_flow(ts, px, vol, buy)  # has open/high/low/close/volume/buy_vol + next_tick
    atrs = calc_atr_series(bars)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, spread ${SPREAD}, {WINDOW}-bar window (matches live cap), FULL period\n")

    all_sigs = make_all_signals(bars)
    for name, sigs in all_sigs.items():
        verify(f"{name} (windowed)", bars, ts, px, atrs, sigs)

    combined = sorted(sum(all_sigs.values(), []))
    verify("PORTFOLIO (all 4, windowed, independent)", bars, ts, px, atrs, combined)


if __name__ == "__main__":
    main()
