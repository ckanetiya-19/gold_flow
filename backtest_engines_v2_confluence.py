"""
GOLDFLOW BACKTEST v2 - WITH TRADITIONAL-TA CONFLUENCE FILTER
============================================================================
Same base engine logic as backtest_engines.py (Quant Sniper, Confluence,
Scalp S1, Scalp S5), but each signal must ALSO pass a "Traditional TA
Confluence" gate before being counted as a trade:

  1. EMA(9,21) trend bias on 5-minute bars (higher timeframe, no lookahead -
     only fully-closed 5-min bars are used)
  2. RSI(14) momentum bias on 5-minute bars (>50 bullish, <50 bearish)
  3. Proximity to recent Swing High/Low + Fibonacci retracement levels on
     1-minute bars (avoid buying into resistance / selling into support)

Rule: at least 2 of these 3 checks must agree with the signal's direction
for the trade to be taken. This script is backtest-only - does NOT touch
the live PORT_9080 system.

This is a SEPARATE file from backtest_engines.py so the original (no
confluence filter) baseline results remain available for side-by-side
comparison.

Run: python backtest_engines_v2_confluence.py
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
        price = float(price)
        volume = float(volume)
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
            current = {
                "time": minute, "open": price, "high": price, "low": price, "close": price,
                "volume": volume, "buy_vol": volume if is_buy else 0.0, "sell_vol": 0.0 if is_buy else volume,
                "cvd": cum_delta, "vwap": round(vwap, 2), "levels": {round(price, 1): volume},
            }
        else:
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += volume
            if is_buy:
                current["buy_vol"] += volume
            else:
                current["sell_vol"] += volume
            current["cvd"] = cum_delta
            current["vwap"] = round(vwap, 2)
            lvl = round(price, 1)
            current["levels"][lvl] = current["levels"].get(lvl, 0) + volume

    if current is not None:
        bars.append(_finalize(current, bars))
    return bars


def _finalize(bar, historical_bars):
    delta = float(bar["buy_vol"] - bar["sell_vol"])
    poc = max(bar["levels"], key=bar["levels"].get) if bar["levels"] else bar["close"]
    avg_vol = np.mean([b["volume"] for b in historical_bars[-20:]]) if len(historical_bars) >= 10 else bar["volume"]
    imbalance = bar["volume"] > (avg_vol * 1.8) if avg_vol > 0 else False

    fvg = None
    if len(historical_bars) >= 2:
        prev2 = historical_bars[-2]
        if prev2["high"] < bar["low"]:
            fvg = "Bullish"
        elif prev2["low"] > bar["high"]:
            fvg = "Bearish"

    phase = "Neutral"
    div = None
    if len(historical_bars) >= 5:
        cvd_vals = [b.get("cvd", 0) for b in historical_bars[-5:]]
        cvd_slope = cvd_vals[-1] - cvd_vals[0]
        price_slope = bar["close"] - historical_bars[-5]["close"]
        if cvd_slope > 0 and price_slope < 0:
            div = "Bullish Divergence"; phase = "Accumulation"
        elif cvd_slope < 0 and price_slope > 0:
            div = "Bearish Divergence"; phase = "Distribution"
        elif cvd_slope > 0:
            phase = "Markup"
        else:
            phase = "Markdown"

    absorption = None
    if abs(delta) > 50 and abs(bar["close"] - bar["open"]) < 0.25:
        absorption = "Bullish Absorption" if delta > 0 else "Bearish Absorption"

    score = 30
    if delta > 0 and bar["close"] > bar["vwap"]: score += 20
    elif delta < 0 and bar["close"] < bar["vwap"]: score += 20
    if imbalance: score += 15
    if fvg is not None: score += 10
    if absorption is not None: score += 15
    if div is not None: score += 10
    confidence = max(5, min(95, score))

    return {**bar, "delta": delta, "poc": poc, "imbalance": imbalance, "fvg": fvg,
            "phase": phase, "confidence": confidence, "absorption": absorption, "cvd_divergence": div}


def calculate_quant_metrics(bars):
    if len(bars) < 16:
        return 50.0, 0.5, 2.0
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    tr_list = []
    for i in range(1, len(bars)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
    if len(tr_list) < 14:
        return 50.0, 0.5, 2.0
    recent_14_tr = tr_list[-14:]
    sum_tr = sum(recent_14_tr)
    atr = sum_tr / 14.0
    range_14 = max(highs[-14:]) - min(lows[-14:])
    if range_14 > 0 and sum_tr > 0:
        ratio = sum_tr / range_14
        chop = 100.0 * (np.log10(ratio) / np.log10(14.0))
        chop = max(0.0, min(100.0, chop))
    else:
        chop = 50.0
    change = abs(closes[-1] - closes[-10])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - 9, len(closes)))
    ker = round(max(0.0, min(1.0, change / path if path > 0 else 0.5)), 3)
    return round(chop, 1), ker, round(atr, 2)


# =============================================================================
# TRADITIONAL TA CONFLUENCE LAYER
# =============================================================================
class TraditionalTA:
    def __init__(self):
        self.five_min_bars = []
        self._current_5m = None
        self.ema9 = None
        self.ema21 = None
        self.closes_5m = []

    def update(self, bar_1m):
        """Call once per finalized 1-minute bar. Only updates EMA/RSI when a
        5-minute bucket CLOSES (causal - no lookahead into the forming bar)."""
        bucket = bar_1m["time"].replace(minute=(bar_1m["time"].minute // 5) * 5, second=0, microsecond=0)
        if self._current_5m is None or self._current_5m["time"] < bucket:
            if self._current_5m is not None:
                self.five_min_bars.append(self._current_5m)
                self._on_5m_close(self._current_5m["close"])
            self._current_5m = {"time": bucket, "open": bar_1m["open"], "high": bar_1m["high"],
                                 "low": bar_1m["low"], "close": bar_1m["close"]}
        else:
            self._current_5m["high"] = max(self._current_5m["high"], bar_1m["high"])
            self._current_5m["low"] = min(self._current_5m["low"], bar_1m["low"])
            self._current_5m["close"] = bar_1m["close"]

    def _on_5m_close(self, close_price):
        self.closes_5m.append(close_price)
        k9 = 2 / (9 + 1)
        k21 = 2 / (21 + 1)
        self.ema9 = close_price if self.ema9 is None else close_price * k9 + self.ema9 * (1 - k9)
        self.ema21 = close_price if self.ema21 is None else close_price * k21 + self.ema21 * (1 - k21)

    def ema_bias(self):
        if self.ema9 is None or self.ema21 is None or len(self.closes_5m) < 21:
            return None
        return "UP" if self.ema9 > self.ema21 else "DOWN"

    def rsi_bias(self, period=14):
        if len(self.closes_5m) < period + 1:
            return None
        deltas = np.diff(self.closes_5m[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        return "BULL" if rsi > 50 else "BEAR"


def swing_and_fib(bars_1m, lookback=30):
    """Simplified, causal (no lookahead) swing high/low + Fibonacci levels
    from the last `lookback` 1-minute bars."""
    if len(bars_1m) < lookback:
        return None
    recent = bars_1m[-lookback:]
    high = max(b["high"] for b in recent)
    low = min(b["low"] for b in recent)
    rng = high - low
    fib_levels = {
        "38.2": high - rng * 0.382,
        "50.0": high - rng * 0.5,
        "61.8": high - rng * 0.618,
    }
    return high, low, fib_levels


def check_traditional_confluence(direction, ta, bars_1m, price):
    """Returns True if >= 2 of the 3 traditional checks agree with direction."""
    checks_passed = 0
    checks_total = 0

    ema_bias = ta.ema_bias()
    if ema_bias is not None:
        checks_total += 1
        if (direction == "BUY" and ema_bias == "UP") or (direction == "SELL" and ema_bias == "DOWN"):
            checks_passed += 1

    rsi_bias = ta.rsi_bias()
    if rsi_bias is not None:
        checks_total += 1
        if (direction == "BUY" and rsi_bias == "BULL") or (direction == "SELL" and rsi_bias == "BEAR"):
            checks_passed += 1

    sr = swing_and_fib(bars_1m)
    if sr is not None:
        high, low, fibs = sr
        rng = high - low
        checks_total += 1
        near_resistance = rng > 0 and abs(price - high) < rng * 0.1
        near_support = rng > 0 and abs(price - low) < rng * 0.1
        sr_ok = True
        if direction == "BUY" and near_resistance:
            sr_ok = False
        if direction == "SELL" and near_support:
            sr_ok = False
        if sr_ok:
            checks_passed += 1

    if checks_total < 3:
        return False  # not enough history yet to judge - be conservative
    return checks_passed >= 2


def run_backtest(bars):
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "rejected_by_ta": 0}
               for name in ["Quant Sniper", "Confluence", "S1_VWAP_MOMENTUM", "S5_MICRO_BREAKOUT"]}

    trade_state = {"in_position": False, "direction": None, "entry_price": 0.0, "sl": 0.0, "tp": 0.0, "engine": None}
    scalp_open = {"S1_VWAP_MOMENTUM": None, "S5_MICRO_BREAKOUT": None}
    ta = TraditionalTA()

    hist = []
    for bar in bars:
        hist.append(bar)
        ta.update(bar)

        if trade_state["in_position"]:
            is_long = trade_state["direction"] in ("BUY", "LONG")
            hit_tp = (bar["high"] >= trade_state["tp"]) if is_long else (bar["low"] <= trade_state["tp"])
            hit_sl = (bar["low"] <= trade_state["sl"]) if is_long else (bar["high"] >= trade_state["sl"])
            if hit_tp or hit_sl:
                exit_p = trade_state["tp"] if hit_tp else trade_state["sl"]
                pnl = (exit_p - trade_state["entry_price"]) if is_long else (trade_state["entry_price"] - exit_p)
                eng = results[trade_state["engine"]]
                eng["trades"] += 1
                eng["pnl"] += pnl
                if pnl > 0: eng["wins"] += 1
                trade_state["in_position"] = False

        for name, pos in list(scalp_open.items()):
            if pos is None:
                continue
            is_long = pos["direction"] == "BUY"
            hit_tp = (bar["high"] >= pos["tp"]) if is_long else (bar["low"] <= pos["tp"])
            hit_sl = (bar["low"] <= pos["sl"]) if is_long else (bar["high"] >= pos["sl"])
            if hit_tp or hit_sl:
                exit_p = pos["tp"] if hit_tp else pos["sl"]
                pnl = (exit_p - pos["entry"]) if is_long else (pos["entry"] - exit_p)
                eng = results[name]
                eng["trades"] += 1
                eng["pnl"] += pnl
                if pnl > 0: eng["wins"] += 1
                scalp_open[name] = None

        if len(hist) < 30:  # need enough for swing/fib + HTF EMA/RSI warmup
            continue

        chop, ker, atr = calculate_quant_metrics(hist)
        is_sideways = (chop >= 60.0) or (ker < 0.25)

        if not trade_state["in_position"]:
            cp = bar["close"]
            vwap = bar["vwap"]
            candle_range = bar["high"] - bar["low"]
            body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
            buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5
            sniper_fired = False
            if not is_sideways:
                if (bar["close"] > bar["open"]) and (cp >= vwap + 0.6) and (body_ratio >= 0.65) and (buy_ratio >= 0.60):
                    if check_traditional_confluence("BUY", ta, hist, cp):
                        sl = round(bar["low"] - 1.8, 2); tp = round(cp + max(3.5, atr * 2.0), 2)
                        trade_state.update({"in_position": True, "direction": "BUY", "entry_price": cp, "sl": sl, "tp": tp, "engine": "Quant Sniper"})
                        sniper_fired = True
                    else:
                        results["Quant Sniper"]["rejected_by_ta"] += 1
                elif (bar["close"] < bar["open"]) and (cp <= vwap - 0.6) and (body_ratio >= 0.65) and (buy_ratio <= 0.40):
                    if check_traditional_confluence("SELL", ta, hist, cp):
                        sl = round(bar["high"] + 1.8, 2); tp = round(cp - max(3.5, atr * 2.0), 2)
                        trade_state.update({"in_position": True, "direction": "SELL", "entry_price": cp, "sl": sl, "tp": tp, "engine": "Quant Sniper"})
                        sniper_fired = True
                    else:
                        results["Quant Sniper"]["rejected_by_ta"] += 1
            if not sniper_fired:
                div = bar.get("cvd_divergence")
                absrp = bar.get("absorption")
                signal = None
                if (div == "Bullish Divergence" or absrp == "Bullish Absorption") and cp > vwap:
                    signal = "BUY"
                elif (div == "Bearish Divergence" or absrp == "Bearish Absorption") and cp < vwap:
                    signal = "SELL"
                if signal and not is_sideways:
                    if check_traditional_confluence(signal, ta, hist, cp):
                        sl = round(cp - 2.0 if signal == "BUY" else cp + 2.0, 2)
                        tp = round(cp + 4.0 if signal == "BUY" else cp - 4.0, 2)
                        trade_state.update({"in_position": True, "direction": signal, "entry_price": cp, "sl": sl, "tp": tp, "engine": "Confluence"})
                    else:
                        results["Confluence"]["rejected_by_ta"] += 1

        candle_range = bar["high"] - bar["low"]
        if candle_range <= 5.0:
            price = bar["close"]
            vwap = bar["vwap"]
            body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
            buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5

            if scalp_open["S1_VWAP_MOMENTUM"] is None:
                is_trending = not is_sideways
                whole_above = bar["low"] > vwap
                whole_below = bar["high"] < vwap
                if is_trending and whole_above and bar["close"] > bar["open"] and body_ratio >= 0.65 and buy_ratio >= 0.60:
                    if check_traditional_confluence("BUY", ta, hist, price):
                        sl = round(bar["low"], 2); tp = round(price + (price - sl) * 1.5, 2)
                        scalp_open["S1_VWAP_MOMENTUM"] = {"direction": "BUY", "entry": price, "sl": sl, "tp": tp}
                    else:
                        results["S1_VWAP_MOMENTUM"]["rejected_by_ta"] += 1
                elif is_trending and whole_below and bar["close"] < bar["open"] and body_ratio >= 0.65 and buy_ratio <= 0.40:
                    if check_traditional_confluence("SELL", ta, hist, price):
                        sl = round(bar["high"], 2); tp = round(price - (sl - price) * 1.5, 2)
                        scalp_open["S1_VWAP_MOMENTUM"] = {"direction": "SELL", "entry": price, "sl": sl, "tp": tp}
                    else:
                        results["S1_VWAP_MOMENTUM"]["rejected_by_ta"] += 1

            if scalp_open["S5_MICRO_BREAKOUT"] is None and len(hist) >= 6:
                lookback = hist[-6:-1]
                recent_high = max(b["high"] for b in lookback)
                recent_low = min(b["low"] for b in lookback)
                avg_vol = sum(b["volume"] for b in lookback) / len(lookback)
                vol_spike = avg_vol > 0 and bar["volume"] > avg_vol * 1.5
                if vol_spike and bar["close"] > recent_high:
                    if check_traditional_confluence("BUY", ta, hist, price):
                        sl = round(recent_low, 2); tp = round(price + (price - sl) * 1.5, 2)
                        scalp_open["S5_MICRO_BREAKOUT"] = {"direction": "BUY", "entry": price, "sl": sl, "tp": tp}
                    else:
                        results["S5_MICRO_BREAKOUT"]["rejected_by_ta"] += 1
                elif vol_spike and bar["close"] < recent_low:
                    if check_traditional_confluence("SELL", ta, hist, price):
                        sl = round(recent_high, 2); tp = round(price - (sl - price) * 1.5, 2)
                        scalp_open["S5_MICRO_BREAKOUT"] = {"direction": "SELL", "entry": price, "sl": sl, "tp": tp}
                    else:
                        results["S5_MICRO_BREAKOUT"]["rejected_by_ta"] += 1

    return results


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}")

    results = run_backtest(bars)

    print("\n" + "=" * 90)
    print("BACKTEST v2 RESULTS - WITH Traditional-TA Confluence Filter (2-of-3: EMA/RSI/S-R+Fib)")
    print("=" * 90)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        print(f"{name:22s} | Trades: {r['trades']:4d} | Win Rate: {wr:5.1f}% | Total PnL: ${r['pnl']:8.2f} | "
              f"Avg/Trade: ${avg:6.2f} | Rejected by TA filter: {r['rejected_by_ta']:4d}")


if __name__ == "__main__":
    main()
