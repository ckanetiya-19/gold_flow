"""
GOLDFLOW BACKTEST v3 - Top-3 strategies COMBINED + Out-of-Sample validation
============================================================================
Standalone. Does NOT touch live PORT_9060/9080.

Top 3 from the individual test (backtest_smc_strategies_v2_full.py):
  A. Volume Imbalance Spike   B. RSI 14 Reversion   C. Fibonacci Bounce

Tests:
  1. Each alone, with in-sample / out-of-sample split (67% / 33% by time)
  2. PORTFOLIO  = A + B + C all fire independently (union of signals)
  3. CONFLUENCE = only trade when >=2 (or all 3) agree on direction within
                  a 5-bar window

Also ranks ALL other strategies on IN-SAMPLE only, then shows their
OUT-SAMPLE, because the top-3 were originally picked using the FULL 8.5
days (including the out-of-sample part) -> selection bias. The honest test
is: "did strategies that looked good in-sample stay good out-of-sample?"

Run: python backtest_smc_strategies_v3_top3_oos.py
"""

from datetime import timedelta
import numpy as np

from backtest_smc_strategies_v2_full import (
    fetch_ticks, build_bars, calc_atr, max_drawdown,
    strategy_volume_imbalance, strategy_rsi_reversion, strategy_fib_bounce,
    strategy_ema_crossover, strategy_macd_crossover, strategy_bollinger_reversion,
    strategy_sr_bounce, strategy_vwap_trend, strategy_absorption, strategy_bar_delta,
    STARTING_BALANCE, SL_ATR_MULT, TP_ATR_MULT, BE_TRIGGER_R, BE_BUFFER,
)
from backtest_smc_strategies_v1 import strategy_fvg_retest, strategy_poc_rejection

WINDOW = 5  # bars for confluence agreement


def simulate(bars, signals):
    """Same ATR SL/TP + 1R auto-breakeven engine as v1/v2, but equity is built
    in EXIT-time order (more accurate drawdown for overlapping trades)."""
    results = []  # (exit_idx, pnl, outcome)
    for sig_idx, direction in signals:
        if sig_idx + 1 >= len(bars):
            continue
        entry = bars[sig_idx]["close"]
        atr = calc_atr(bars, sig_idx)
        if atr <= 0:
            continue
        is_long = direction == "BUY"
        sl = entry - SL_ATR_MULT * atr if is_long else entry + SL_ATR_MULT * atr
        tp = entry + TP_ATR_MULT * atr if is_long else entry - TP_ATR_MULT * atr
        risk = abs(entry - sl)
        be_price = entry + BE_TRIGGER_R * risk if is_long else entry - BE_TRIGGER_R * risk
        be_armed = False
        cur_sl = sl
        for j in range(sig_idx + 1, len(bars)):
            b = bars[j]
            if is_long:
                if not be_armed and b["high"] >= be_price:
                    be_armed = True; cur_sl = entry + BE_BUFFER
                hit_tp = b["high"] >= tp; hit_sl = b["low"] <= cur_sl
            else:
                if not be_armed and b["low"] <= be_price:
                    be_armed = True; cur_sl = entry - BE_BUFFER
                hit_tp = b["low"] <= tp; hit_sl = b["high"] >= cur_sl
            if hit_tp or hit_sl:
                exit_p = tp if hit_tp else cur_sl
                pnl = (exit_p - entry) if is_long else (entry - exit_p)
                outcome = "W" if hit_tp else ("BE" if be_armed else "L")
                results.append((j, pnl, outcome))
                break

    results.sort(key=lambda x: x[0])
    equity = [STARTING_BALANCE]
    w = l = be = 0
    for _, pnl, oc in results:
        equity.append(equity[-1] + pnl)
        if oc == "W": w += 1
        elif oc == "L": l += 1
        else: be += 1
    total = w + l + be
    dd, dd_pct = max_drawdown(equity)
    return {"trades": total, "w": w, "l": l, "be": be,
            "wr": (w / total * 100) if total else 0.0,
            "pnl": equity[-1] - STARTING_BALANCE, "dd_pct": dd_pct}


def split_signals(signals, split_idx):
    ins = [s for s in signals if s[0] < split_idx]
    oos = [s for s in signals if s[0] >= split_idx]
    return ins, oos


def confluence(sig_lists, min_agree, window=WINDOW, cooldown=5):
    """Fire when >= min_agree different strategies signal the same direction
    within `window` bars. Fires at the bar of the latest agreeing signal."""
    tagged = []
    for k, sigs in enumerate(sig_lists):
        for idx, d in sigs:
            tagged.append((idx, d, k))
    tagged.sort()
    out = []
    last_fire = -999
    for i, (idx, d, k) in enumerate(tagged):
        if idx - last_fire < cooldown:
            continue
        agree = {k}
        for idx2, d2, k2 in tagged:
            if idx - window <= idx2 <= idx and d2 == d:
                agree.add(k2)
        if len(agree) >= min_agree:
            out.append((idx, d)); last_fire = idx
    return out


def fmt(r):
    return (f"Trades {r['trades']:4d} | W:{r['w']:3d} L:{r['l']:3d} BE:{r['be']:3d} | "
            f"WR {r['wr']:5.1f}% | PnL ${r['pnl']:8.2f} | MaxDD {r['dd_pct']:5.1f}%")


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    total_sec = (bars[-1]["time"] - bars[0]["time"]).total_seconds()
    split_time = bars[0]["time"] + timedelta(seconds=total_sec * 0.67)
    split_idx = next(i for i, b in enumerate(bars) if b["time"] >= split_time)
    print(f"{len(bars)} bars, {bars[0]['time']} -> {bars[-1]['time']} ({total_sec/86400:.2f} days)")
    print(f"Split at {split_time} (in-sample {split_idx} bars / out-of-sample {len(bars)-split_idx} bars)\n")

    A = strategy_volume_imbalance(bars)
    B = strategy_rsi_reversion(bars)
    C = strategy_fib_bounce(bars)

    print("=" * 112)
    print("PART 1: TOP-3 INDIVIDUALLY - full / in-sample / out-of-sample")
    print("=" * 112)
    for name, sigs in (("A. Volume Imbalance Spike", A), ("B. RSI 14 Reversion", B), ("C. Fibonacci Bounce", C)):
        ins, oos = split_signals(sigs, split_idx)
        print(f"{name}")
        print(f"   FULL      : {fmt(simulate(bars, sigs))}")
        print(f"   IN-SAMPLE : {fmt(simulate(bars, ins))}")
        print(f"   OUT-SAMPLE: {fmt(simulate(bars, oos))}\n")

    print("=" * 112)
    print("PART 2: COMBINED - Portfolio (A+B+C independent) and Confluence (agreement required)")
    print("=" * 112)
    portfolio = sorted(A + B + C)
    conf2 = confluence([A, B, C], min_agree=2)
    conf3 = confluence([A, B, C], min_agree=3)
    for name, sigs in (("PORTFOLIO (A+B+C all fire)", portfolio),
                       ("CONFLUENCE 2-of-3 agree", conf2),
                       ("CONFLUENCE 3-of-3 agree", conf3)):
        ins, oos = split_signals(sigs, split_idx)
        print(f"{name}  ({len(sigs)} signals)")
        print(f"   FULL      : {fmt(simulate(bars, sigs))}")
        print(f"   IN-SAMPLE : {fmt(simulate(bars, ins))}")
        print(f"   OUT-SAMPLE: {fmt(simulate(bars, oos))}\n")

    print("=" * 112)
    print("PART 3: SELECTION-BIAS CHECK - all strategies ranked on IN-SAMPLE only, then shown OUT-OF-SAMPLE")
    print("=" * 112)
    all_strats = {
        "Volume Imbalance Spike": A, "RSI 14 Reversion": B, "Fibonacci Bounce": C,
        "MACD Crossover": strategy_macd_crossover(bars),
        "Bollinger Reversion": strategy_bollinger_reversion(bars),
        "Support/Resistance": strategy_sr_bounce(bars),
        "VWAP Trend Position": strategy_vwap_trend(bars),
        "Absorption Detection": strategy_absorption(bars),
        "Bar Delta Confirmation": strategy_bar_delta(bars),
        "EMA Crossover": strategy_ema_crossover(bars),
        "POC Rejection": strategy_poc_rejection(bars),
        "FVG Retest": strategy_fvg_retest(bars),
    }
    rows = []
    for name, sigs in all_strats.items():
        ins, oos = split_signals(sigs, split_idx)
        rows.append((name, simulate(bars, ins), simulate(bars, oos)))
    rows.sort(key=lambda r: r[1]["pnl"], reverse=True)
    print(f"{'Strategy (ranked by IN-SAMPLE PnL)':28s} | {'IN PnL':>9s} {'IN DD':>6s} | {'OUT PnL':>9s} {'OUT DD':>6s} | {'OUT WR':>6s} | OUT trades")
    for name, ri, ro in rows:
        print(f"{name:28s} | ${ri['pnl']:8.2f} {ri['dd_pct']:5.1f}% | ${ro['pnl']:8.2f} {ro['dd_pct']:5.1f}% | {ro['wr']:5.1f}% | {ro['trades']}")


if __name__ == "__main__":
    main()
