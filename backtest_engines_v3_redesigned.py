"""
GOLDFLOW BACKTEST v3 - REDESIGNED ENTRY LOGIC (root-cause fix)
============================================================================
Root cause found in v1 backtest: Quant Sniper's SL was anchored to
"bar's low/high - 1.8" instead of a controlled distance from ENTRY price.
Since Quant Sniper requires strong-bodied candles (close near one extreme),
the bar's low/high can be well below/above the close, making the REAL
risk distance 2-4x wider than the intended 1.8 - breaking the assumed
Risk:Reward ratio and explaining the backtest losses despite an
above-breakeven win rate under the naive assumption.

v3 changes (backtest-only, does NOT touch live PORT_9080):
  1. SL/TP anchored to ENTRY price with a clean, fixed multiple - not to
     bar extremes + a buffer. Guarantees the designed Risk:Reward is the
     REAL Risk:Reward.
  2. Entry thresholds ATR-normalized (e.g. "0.3 x ATR" instead of a fixed
     $0.6) so the strategy adapts to changing volatility instead of using
     one-size-fits-all dollar thresholds.
  3. Tighter, more selective volume/body confirmation to raise signal
     quality over quantity.

Run: python backtest_engines_v3_redesigned.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"


def questdb_query(sql):
    url = QUESTDB_URL + "?" + urllib.parse.urlencode({"query": sql})
    with urllib.request.urlopen(url, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("dataset", [])


def fetch_ticks():
    print("Fetching tick history from QuestDB...")
    rows = questdb_query("SELECT side, price, volume, timestamp FROM ticks_v2 WHERE symbol='XAUUSD' ORDER BY timestamp")
    print(f"{len(rows)} ticks fetched.")
    return rows


def build_bars(ticks):
    bars = []
    current = None
    cum_delta = 0.0
    cum_pv = 0.0
    cum_vol = 0.0
    for side, price, volume, ts in ticks:
        price = float(price); volume = float(volume)
        is_buy = (side == "BUY")
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        minute = ts_dt.replace(second=0, microsecond=0)
        cum_delta += volume if is_buy else -volume
        cum_pv += price * volume
        cum_vol += volume
        vwap = (cum_pv / cum_vol) if cum_vol > 0 else price
        if current is None or current["time"] < minute:
            if current is not None:
                bars.append(_finalize(current, bars))
            current = {"time": minute, "open": price, "high": price, "low": price, "close": price,
                       "volume": volume, "buy_vol": volume if is_buy else 0.0, "sell_vol": 0.0 if is_buy else volume,
                       "cvd": cum_delta, "vwap": round(vwap, 2), "levels": {round(price, 1): volume}}
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += volume
            if is_buy: current["buy_vol"] += volume
            else: current["sell_vol"] += volume
            current["cvd"] = cum_delta
            current["vwap"] = round(vwap, 2)
            lvl = round(price, 1)
            current["levels"][lvl] = current["levels"].get(lvl, 0) + volume
    if current is not None:
        bars.append(_finalize(current, bars))
    return bars


def _finalize(bar, historical_bars):
    delta = float(bar["buy_vol"] - bar["sell_vol"])
    avg_vol = np.mean([b["volume"] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar["volume"]
    imbalance = bar["volume"] > (avg_vol * 1.8) if avg_vol > 0 else False
    absorption = None
    if abs(delta) > 50 and abs(bar["close"] - bar["open"]) < 0.25:
        absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption"
    div = None
    if len(historical_bars) >= 5:
        cvd_vals = [b.get("cvd", 0) for b in historical_bars[-5:]]
        cvd_slope = cvd_vals[-1] - cvd_vals[0]
        price_slope = bar["close"] - historical_bars[-5]["close"]
        if cvd_slope > 0 and price_slope < 0: div = "Bullish Divergence"
        elif cvd_slope < 0 and price_slope > 0: div = "Bearish Divergence"
    return {**bar, "delta": delta, "imbalance": imbalance, "absorption": absorption, "cvd_divergence": div}


def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return 2.0
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def calc_chop_ker(bars):
    if len(bars) < 16:
        return 50.0, 0.5
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    tr_list = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(bars))]
    if len(tr_list) < 14:
        return 50.0, 0.5
    sum_tr = sum(tr_list[-14:])
    range_14 = max(highs[-14:]) - min(lows[-14:])
    chop = max(0.0, min(100.0, 100.0 * (np.log10(sum_tr/range_14) / np.log10(14.0)))) if range_14 > 0 and sum_tr > 0 else 50.0
    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[i]-closes[i-1]) for i in range(len(closes)-9, len(closes)))
    ker = round(max(0.0, min(1.0, change/path if path > 0 else 0.5)), 3)
    return round(chop, 1), ker


def run_backtest_v3(bars):
    """
    REDESIGNED entry logic:
      - Entry threshold: price vs VWAP measured in ATR units (adapts to volatility)
      - SL/TP: fixed, clean multiples of ATR from ENTRY price (real, predictable R:R)
      - Two variants tested side by side: R:R 1:1.5 and R:R 1:1.2 (tighter target,
        since a higher win rate at a smaller R:R may suit this instrument's
        noisy, fast-reverting behavior better than chasing bigger R:R)
    """
    variants = {
        "V3_RR_1.5": {"sl_mult": 1.0, "tp_mult": 1.5},
        "V3_RR_1.2": {"sl_mult": 1.0, "tp_mult": 1.2},
        "V3_RR_2.0": {"sl_mult": 1.0, "tp_mult": 2.0},
    }
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0} for name in variants}
    open_pos = {name: None for name in variants}

    hist = []
    for bar in bars:
        hist.append(bar)

        for name, pos in list(open_pos.items()):
            if pos is None:
                continue
            is_long = pos["direction"] == "BUY"
            hit_tp = (bar["high"] >= pos["tp"]) if is_long else (bar["low"] <= pos["tp"])
            hit_sl = (bar["low"] <= pos["sl"]) if is_long else (bar["high"] >= pos["sl"])
            if hit_tp or hit_sl:
                exit_p = pos["tp"] if hit_tp else pos["sl"]
                pnl = (exit_p - pos["entry"]) if is_long else (pos["entry"] - exit_p)
                results[name]["trades"] += 1
                results[name]["pnl"] += pnl
                if pnl > 0: results[name]["wins"] += 1
                open_pos[name] = None

        if len(hist) < 20:
            continue

        atr = calc_atr(hist)
        chop, ker = calc_chop_ker(hist)
        is_trending = not ((chop >= 60.0) or (ker < 0.25))

        cp = bar["close"]
        vwap = bar["vwap"]
        candle_range = bar["high"] - bar["low"]
        body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
        buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5

        # ATR-normalized momentum entry (replaces fixed $0.6 threshold)
        direction = None
        if is_trending and atr > 0:
            if (bar["close"] > bar["open"]) and (cp >= vwap + 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio >= 0.58):
                direction = "BUY"
            elif (bar["close"] < bar["open"]) and (cp <= vwap - 0.3 * atr) and (body_ratio >= 0.60) and (buy_ratio <= 0.42):
                direction = "SELL"

        if direction is not None:
            for name, cfg in variants.items():
                if open_pos[name] is not None:
                    continue
                sl_dist = cfg["sl_mult"] * atr
                tp_dist = cfg["tp_mult"] * atr
                sl = round(cp - sl_dist, 2) if direction == "BUY" else round(cp + sl_dist, 2)
                tp = round(cp + tp_dist, 2) if direction == "BUY" else round(cp - tp_dist, 2)
                open_pos[name] = {"direction": direction, "entry": cp, "sl": sl, "tp": tp}

    return results


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}")

    results = run_backtest_v3(bars)

    print("\n" + "=" * 90)
    print("BACKTEST v3 RESULTS - Redesigned entry (ATR-normalized) + clean entry-anchored SL/TP")
    print("=" * 90)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        print(f"{name:15s} | Trades: {r['trades']:4d} | Win Rate: {wr:5.1f}% | Total PnL: ${r['pnl']:8.2f} | Avg/Trade: ${avg:6.2f}")


if __name__ == "__main__":
    main()
