"""Type 1 (ADX-HIGH trend-confirmation, VWAP-side + order-flow) vs Type 2 (ADX-LOW fade, already
built as strategy_adx_vwap_fade). Same Trailing exit ($2/$1, SL=1xATR), verified against random
baseline, FULL period.
Run: python backtest_adx_type1_vs_type2.py
"""
import random

from backtest_9080_all_strategies_oos import load_ticks, pnl_of
from backtest_vwap_first_close import market_closed
from backtest_iteration_round1 import build_bars_full, calc_atr_series, run_trailing_exit, strategy_adx_vwap_fade, SPREAD, SL_ATR_MULT


def build_bars_with_flow(ts, px, vol, buy, bucket_s=60):
    bars = []
    cur = None
    for i in range(len(ts)):
        bucket = int(ts[i] // bucket_s) * bucket_s
        p, v, b_ = px[i], vol[i], buy[i]
        if cur is None or cur["t"] < bucket:
            if cur is not None:
                cur["next_tick"] = i
                bars.append(cur)
            cur = {"t": bucket, "open": p, "high": p, "low": p, "close": p, "volume": v,
                   "buy_vol": v if b_ else 0.0}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += v
            if b_:
                cur["buy_vol"] += v
    return bars


def compute_adx_vwap(bars, adx_period=14):
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
        out[period] = sum(series[1:period + 1])
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

    cum_pv = cum_vol = 0.0
    vwaps = [0.0] * n
    for i, b in enumerate(bars):
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += tp * b["volume"]
        cum_vol += b["volume"]
        vwaps[i] = cum_pv / cum_vol if cum_vol > 0 else tp
    return adx, vwaps


def strategy_adx_vwap_trend(bars, adx_period=14, adx_high=25.0):
    """Type 1: ADX HIGH (strong trend) + price on trending side of VWAP + order-flow (buy_vol
    ratio) confirms same direction + strong-bodied candle -> trade WITH the trend."""
    adx, vwaps = compute_adx_vwap(bars, adx_period)
    signals = []
    cooldown_until = -1
    for i in range(adx_period * 2, len(bars) - 1):
        if i <= cooldown_until:
            continue
        b = bars[i]
        candle_range = b["high"] - b["low"]
        body_ratio = (abs(b["close"] - b["open"]) / candle_range) if candle_range > 0 else 0.0
        buy_ratio = (b["buy_vol"] / b["volume"]) if b["volume"] > 0 else 0.5
        if adx[i] < adx_high:
            continue
        if b["close"] > vwaps[i] and b["close"] > b["open"] and body_ratio >= 0.55 and buy_ratio >= 0.58:
            signals.append((i, "BUY")); cooldown_until = i + 5
        elif b["close"] < vwaps[i] and b["close"] < b["open"] and body_ratio >= 0.55 and buy_ratio <= 0.42:
            signals.append((i, "SELL")); cooldown_until = i + 5
    return signals


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
    bars_flow = build_bars_with_flow(ts, px, vol, buy)
    bars_plain = build_bars_full(ts, px, vol, 60)
    atrs = calc_atr_series(bars_plain)
    print(f"{(ts[-1]-ts[0])/86400:.2f} days, spread ${SPREAD}, FULL period only\n")

    print("=" * 100)
    print("TYPE 1: ADX-HIGH trend-confirmation (VWAP-side + order-flow confirm)")
    print("=" * 100)
    t1_sigs = strategy_adx_vwap_trend(bars_flow)
    verify("Type 1 (ADX>25, trend-follow)", bars_flow, ts, px, atrs, t1_sigs)

    print("\n" + "=" * 100)
    print("TYPE 2: ADX-LOW momentum-exhaustion fade (already built)")
    print("=" * 100)
    t2_sigs = strategy_adx_vwap_fade(bars_plain)
    verify("Type 2 (ADX<20, fade)", bars_plain, ts, px, atrs, t2_sigs)


if __name__ == "__main__":
    main()
