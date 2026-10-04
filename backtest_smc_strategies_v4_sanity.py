"""
Sanity checks for the v3 top-3 result (standalone, read-only):
  1. RANDOM-ENTRY baseline: random bars + random direction, same SL/TP/BE
     engine. If random entries also "profit", the signals add nothing.
  2. SPREAD/COST sensitivity: subtract a per-trade cost from PnL.
Run: python backtest_smc_strategies_v4_sanity.py
"""
import random
from datetime import timedelta

from backtest_smc_strategies_v2_full import (
    fetch_ticks, build_bars, strategy_volume_imbalance, strategy_rsi_reversion, strategy_fib_bounce,
)
from backtest_smc_strategies_v3_top3_oos import simulate, split_signals

def live_spread():
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            si = mt5.symbol_info("XAUUSD.sd")
            if si is not None:
                return si.spread * si.point
    except Exception:
        pass
    return None


def main():
    bars = build_bars(fetch_ticks())
    total_sec = (bars[-1]["time"] - bars[0]["time"]).total_seconds()
    split_time = bars[0]["time"] + timedelta(seconds=total_sec * 0.67)
    split_idx = next(i for i, b in enumerate(bars) if b["time"] >= split_time)

    A = strategy_volume_imbalance(bars); B = strategy_rsi_reversion(bars); C = strategy_fib_bounce(bars)
    portfolio = sorted(A + B + C)

    print("=" * 100)
    print("CHECK 1: RANDOM-ENTRY BASELINE (same SL/TP/BE engine, 15 random seeds)")
    print("=" * 100)
    for label, n in (("~Volume-Imbalance-sized (545)", 545), ("~Portfolio-sized (1346)", 1346)):
        fulls, oos_pnls = [], []
        for seed in range(15):
            rnd = random.Random(seed)
            sigs = sorted((rnd.randrange(30, len(bars) - 2), rnd.choice(["BUY", "SELL"])) for _ in range(n))
            fulls.append(simulate(bars, sigs)["pnl"])
            _, oos = split_signals(sigs, split_idx)
            oos_pnls.append(simulate(bars, oos)["pnl"])
        print(f"{label:32s} FULL PnL: mean ${sum(fulls)/len(fulls):8.2f}  range [${min(fulls):.2f} .. ${max(fulls):.2f}]"
              f" | OOS PnL: mean ${sum(oos_pnls)/len(oos_pnls):8.2f}  range [${min(oos_pnls):.2f} .. ${max(oos_pnls):.2f}]")

    spread = live_spread()
    print("\n" + "=" * 100)
    print(f"CHECK 2: COST SENSITIVITY  (live XAUUSD.sd spread from MT5: {('$%.2f' % spread) if spread else 'unavailable'})")
    print("=" * 100)
    costs = sorted(set([0.0, 0.10, 0.20, 0.30] + ([round(spread, 2)] if spread else [])))
    sets = (("A. Volume Imbalance", A), ("B. RSI 14", B), ("C. Fibonacci", C), ("PORTFOLIO A+B+C", portfolio))
    print(f"{'Strategy':22s} | " + " | ".join(f"cost ${c:.2f} (FULL / OOS)" for c in costs))
    for name, sigs in sets:
        full = simulate(bars, sigs)
        _, oos_s = split_signals(sigs, split_idx)
        oos = simulate(bars, oos_s)
        cells = [f"${full['pnl'] - c * full['trades']:8.2f} / ${oos['pnl'] - c * oos['trades']:7.2f}" for c in costs]
        print(f"{name:22s} | " + " | ".join(cells))
    print("\nAvg PnL/trade before costs: " + ", ".join(
        f"{n}: ${simulate(bars, s)['pnl'] / max(1, simulate(bars, s)['trades']):.3f}" for n, s in sets))


if __name__ == "__main__":
    main()
