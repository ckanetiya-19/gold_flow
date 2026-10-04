"""
Iteration round 1: retest our best-known candidates (RSI-14 Reversion, Volume Imbalance Spike -
both showed the best PnL/MaxDD ratio in the earlier 15-strategy sweep, tested there with fixed
2xSL TP+BE) using TRAILING exit instead ($2 arm / $1 trail - the mechanism that improved V1 on
1-min). Also adds a new SSRN-inspired idea: ADX-conditioned VWAP fade (momentum-exhaustion
reversal), from "Momentum Exhaustion and Fair Value Reversion: An ADX-conditioned VWAP Strategy"
(Bhatti, SSRN). Real tick data, spread $0.24, tick-level exits, FULL period.

Run: python backtest_iteration_round1.py
"""
import numpy as np

from backtest_9080_all_strategies_oos import load_ticks, pnl_of, run_exit
from backtest_vwap_first_close import market_closed
from backtest_smc_strategies_v2_full import strategy_rsi_reversion, strategy_volume_imbalance

SPREAD = 0.24
TRAIL_ARM = 2.0
TRAIL_DIST = 1.0
SL_ATR_MULT = 1.0


def calc_atr_series(bars, period=14):
    atrs = [2.0] * len(bars)
    for i in range(period, len(bars)):
        trs = []
        for j in range(i - period + 1, i + 1):
            h, l, pc = bars[j]["high"], bars[j]["low"], bars[j - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atrs[i] = sum(trs) / len(trs)
    return atrs


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


def build_bars_full(ts, px, vol, bucket_s):
    bars = []
    cur = None
    for i in range(len(ts)):
        bucket = int(ts[i] // bucket_s) * bucket_s
        p, v = px[i], vol[i]
        if cur is None or cur["t"] < bucket:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": bucket, "open": p, "high": p, "low": p, "close": p, "volume": v}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
    return bars


def main():
    ts, px, vol, buy = load_ticks()
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, spread ${SPREAD}, FULL period\n")

    bars = build_bars_full(ts, px, vol, 60)  # has volume + next_tick fields
    atrs = calc_atr_series(bars)

    print("=" * 100)
    print("A) RSI-14 Reversion + TRAILING exit ($2 arm / $1 trail, SL = 1x ATR)")
    print("=" * 100)
    rsi_sigs = strategy_rsi_reversion(bars)
    run_and_report("RSI-14 + Trailing", bars, ts, px, atrs, rsi_sigs)

    print("\n" + "=" * 100)
    print("B) Volume Imbalance Spike + TRAILING exit")
    print("=" * 100)
    vi_sigs = strategy_volume_imbalance(bars)
    run_and_report("VolImbalance + Trailing", bars, ts, px, atrs, vi_sigs)

    print("\n" + "=" * 100)
    print("C) ADX-conditioned VWAP fade (new SSRN idea: momentum exhaustion reversal)")
    print("=" * 100)
    adx_sigs = strategy_adx_vwap_fade(bars)
    run_and_report("ADX-VWAP-Fade + Trailing", bars, ts, px, atrs, adx_sigs)


def run_and_report(label, bars, ts, px, atrs, signals):
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
    n = len(trades)
    if n == 0:
        print(f"{label}: no trades")
        return
    wins = sum(1 for x in trades if x["why"] == "TRAIL")
    gross = sum(x["pnl"] for x in trades)
    net = gross - SPREAD * n
    eq, peak, dd = 100.0, 100.0, 0.0
    for x in sorted(trades, key=lambda z: z["t"]):
        eq += x["pnl"] - SPREAD
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak > 0 else 0)
    print(f"{label}: trades {n:4d} | trail-exit {wins:4d} ({wins/n*100:.1f}%) | gross ${gross:8.2f} | "
          f"NET ${net:8.2f} | net/trade ${net/n:6.3f} | MaxDD {dd:5.1f}% | PnL/DD ratio: {net/dd if dd>0 else float('inf'):.2f}")


def strategy_adx_vwap_fade(bars, adx_period=14, adx_low=20.0, dev_atr_mult=1.5):
    """When ADX is LOW (weak/exhausted trend) and price has deviated far from VWAP (in ATR terms),
    fade back toward VWAP (mean-reversion), per the 'momentum exhaustion' SSRN concept."""
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]
    n = len(bars)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    def smoothed(series, period):
        out = [0.0] * n
        s = sum(series[1:period + 1])
        out[period] = s
        for i in range(period + 1, n):
            out[i] = out[i - 1] - out[i - 1] / period + series[i]
        return out

    atr_sm = smoothed(tr, adx_period)
    pdm_sm = smoothed(plus_dm, adx_period)
    mdm_sm = smoothed(minus_dm, adx_period)
    adx = [0.0] * n
    dx_hist = []
    for i in range(adx_period, n):
        if atr_sm[i] <= 0:
            continue
        pdi = 100 * pdm_sm[i] / atr_sm[i]
        mdi = 100 * mdm_sm[i] / atr_sm[i]
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
        dx_hist.append(dx)
        if len(dx_hist) >= adx_period:
            adx[i] = sum(dx_hist[-adx_period:]) / adx_period

    atrs = calc_atr_series(bars)
    vwap_cum_pv = vwap_cum_vol = 0.0
    vwaps = [0.0] * n
    for i, b in enumerate(bars):
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        vwap_cum_pv += tp * b["volume"]
        vwap_cum_vol += b["volume"]
        vwaps[i] = vwap_cum_pv / vwap_cum_vol if vwap_cum_vol > 0 else tp

    signals = []
    cooldown_until = -1
    for i in range(adx_period * 2, n - 1):
        if i <= cooldown_until:
            continue
        dev = closes[i] - vwaps[i]
        atr = atrs[i] if atrs[i] > 0 else 2.0
        if adx[i] > 0 and adx[i] < adx_low and abs(dev) > dev_atr_mult * atr:
            direction = "SELL" if dev > 0 else "BUY"   # fade back toward VWAP
            signals.append((i, direction))
            cooldown_until = i + 5
    return signals


if __name__ == "__main__":
    main()
