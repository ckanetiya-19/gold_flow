"""
GOLDFLOW BACKTEST ENGINE
============================================================================
Replays QuestDB's stored real tick history (ticks_v2, real Binance PAXG
aggTrade prints tagged symbol='XAUUSD') through the EXACT SAME signal logic
currently running live in PORT_9080_AI_QUANT_TERMINAL_V2.py, to evaluate how
each of the 7 bar-based engines would have performed historically - BEFORE
deciding whether to add any new features (per user's explicit request,
2026-09-18: backtest first, decide on changes after).

Engines backtested (bar-close driven, reconstructable from tick history):
  - Quant Sniper (VWAP momentum sniper)
  - Confluence (CVD-divergence / absorption backup)
  - Scalp S1: VWAP-Cross Momentum
  - Scalp S2: Band Extreme + Absorption Reversal
  - Scalp S3: Order-Book Imbalance Spike  <-- SKIPPED, needs live depth data
  - Scalp S4: Tape Momentum              <-- SKIPPED, needs live tape data
  - Scalp S5: Micro-Breakout

NOT backtested (data not available historically):
  - HFT-Engine: needs 100ms depth-snapshot history (bid/ask book), which
    QuestDB never stored (only aggTrade ticks). Would need depth logging
    added now, then re-run backtest after enough history accumulates.
  - Scalp S3 (Imbalance) and S4 (Tape Momentum): these read the LIVE
    order_book/recent_tape state, which also isn't durably stored.

IMPORTANT CAVEAT: ticks_v2 is Binance PAXG price data (the volume/CVD
source), not iTick real spot XAUUSD. Backtest P&L is therefore approximate,
same caveat as the earlier MFE analysis.

Run: python backtest_engines.py
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
    """Reconstructs 1-minute bars with the same fields finalize_bar() in
    PORT_9080 computes, from raw tick history."""
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
    """Mirrors finalize_bar() in PORT_9080_AI_QUANT_TERMINAL_V2.py exactly."""
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
    """Mirrors calculate_quant_metrics() in PORT_9080."""
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


def run_backtest(bars):
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "pnl_list": []}
               for name in ["Quant Sniper", "Confluence", "S1_VWAP_MOMENTUM", "S2_BAND_ABSORPTION", "S5_MICRO_BREAKOUT"]}

    trade_state = {"in_position": False, "direction": None, "entry_price": 0.0, "sl": 0.0, "tp": 0.0, "engine": None}
    scalp_open = {"S1_VWAP_MOMENTUM": None, "S2_BAND_ABSORPTION": None, "S5_MICRO_BREAKOUT": None}

    hist = []
    for i, bar in enumerate(bars):
        hist.append(bar)

        # --- check exits for shared trade_state (Sniper/Confluence) ---
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
                eng["pnl_list"].append(pnl)
                if pnl > 0: eng["wins"] += 1
                trade_state["in_position"] = False

        # --- check exits for independent scalp positions ---
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
                eng["pnl_list"].append(pnl)
                if pnl > 0: eng["wins"] += 1
                scalp_open[name] = None

        if len(hist) < 16:
            continue

        chop, ker, atr = calculate_quant_metrics(hist)
        is_sideways = (chop >= 60.0) or (ker < 0.25)

        # --- Quant Sniper + Confluence (evaluate_dual_engines logic) ---
        if not trade_state["in_position"]:
            cp = bar["close"]
            vwap = bar["vwap"]
            candle_range = bar["high"] - bar["low"]
            body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
            buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5
            sniper_fired = False
            if not is_sideways:
                if (bar["close"] > bar["open"]) and (cp >= vwap + 0.6) and (body_ratio >= 0.65) and (buy_ratio >= 0.60):
                    sl = round(bar["low"] - 1.8, 2)
                    tp = round(cp + max(3.5, atr * 2.0), 2)
                    trade_state.update({"in_position": True, "direction": "BUY", "entry_price": cp, "sl": sl, "tp": tp, "engine": "Quant Sniper"})
                    sniper_fired = True
                elif (bar["close"] < bar["open"]) and (cp <= vwap - 0.6) and (body_ratio >= 0.65) and (buy_ratio <= 0.40):
                    sl = round(bar["high"] + 1.8, 2)
                    tp = round(cp - max(3.5, atr * 2.0), 2)
                    trade_state.update({"in_position": True, "direction": "SELL", "entry_price": cp, "sl": sl, "tp": tp, "engine": "Quant Sniper"})
                    sniper_fired = True
            if not sniper_fired:
                div = bar.get("cvd_divergence")
                absrp = bar.get("absorption")
                signal = None
                if (div == "Bullish Divergence" or absrp == "Bullish Absorption") and cp > vwap:
                    signal = "BUY"
                elif (div == "Bearish Divergence" or absrp == "Bearish Absorption") and cp < vwap:
                    signal = "SELL"
                if signal and not is_sideways:
                    sl = round(cp - 2.0 if signal == "BUY" else cp + 2.0, 2)
                    tp = round(cp + 4.0 if signal == "BUY" else cp - 4.0, 2)
                    trade_state.update({"in_position": True, "direction": signal, "entry_price": cp, "sl": sl, "tp": tp, "engine": "Confluence"})

        # --- Scalp strategies (evaluate_scalp_strategies logic) ---
        candle_range = bar["high"] - bar["low"]
        if candle_range <= 5.0:
            price = bar["close"]
            vwap = bar["vwap"]
            body_ratio = (abs(bar["close"] - bar["open"]) / candle_range) if candle_range > 0 else 0.0
            buy_ratio = (bar["buy_vol"] / bar["volume"]) if bar["volume"] > 0 else 0.5

            # S1: VWAP-Cross Momentum
            if scalp_open["S1_VWAP_MOMENTUM"] is None:
                is_trending = not is_sideways
                whole_above = bar["low"] > vwap
                whole_below = bar["high"] < vwap
                if is_trending and whole_above and bar["close"] > bar["open"] and body_ratio >= 0.65 and buy_ratio >= 0.60:
                    sl = round(bar["low"], 2); tp = round(price + (price - sl) * 1.5, 2)
                    scalp_open["S1_VWAP_MOMENTUM"] = {"direction": "BUY", "entry": price, "sl": sl, "tp": tp}
                elif is_trending and whole_below and bar["close"] < bar["open"] and body_ratio >= 0.65 and buy_ratio <= 0.40:
                    sl = round(bar["high"], 2); tp = round(price - (sl - price) * 1.5, 2)
                    scalp_open["S1_VWAP_MOMENTUM"] = {"direction": "SELL", "entry": price, "sl": sl, "tp": tp}

            # S2: Band Extreme + Absorption Reversal
            if scalp_open["S2_BAND_ABSORPTION"] is None:
                vwap_now, upper_band, lower_band = _compute_bands(hist)
                absorption = bar.get("absorption")
                if bar["low"] <= lower_band and absorption == "Bullish Absorption":
                    scalp_open["S2_BAND_ABSORPTION"] = {"direction": "BUY", "entry": price, "sl": round(bar["low"], 2), "tp": vwap_now}
                elif bar["high"] >= upper_band and absorption == "Bearish Absorption":
                    scalp_open["S2_BAND_ABSORPTION"] = {"direction": "SELL", "entry": price, "sl": round(bar["high"], 2), "tp": vwap_now}

            # S5: Micro-Breakout
            if scalp_open["S5_MICRO_BREAKOUT"] is None and len(hist) >= 6:
                lookback = hist[-6:-1]
                recent_high = max(b["high"] for b in lookback)
                recent_low = min(b["low"] for b in lookback)
                avg_vol = sum(b["volume"] for b in lookback) / len(lookback)
                vol_spike = avg_vol > 0 and bar["volume"] > avg_vol * 1.5
                if vol_spike and bar["close"] > recent_high:
                    sl = round(recent_low, 2); tp = round(price + (price - sl) * 1.5, 2)
                    scalp_open["S5_MICRO_BREAKOUT"] = {"direction": "BUY", "entry": price, "sl": sl, "tp": tp}
                elif vol_spike and bar["close"] < recent_low:
                    sl = round(recent_high, 2); tp = round(price - (sl - price) * 1.5, 2)
                    scalp_open["S5_MICRO_BREAKOUT"] = {"direction": "SELL", "entry": price, "sl": sl, "tp": tp}

    return results


def _compute_bands(bars):
    recent = bars[-100:] if len(bars) > 100 else bars
    cum_pv = 0.0; cum_vol = 0.0; cum_diffsq = 0.0
    vwap_now = recent[-1]["close"]
    for b in recent:
        tp = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b.get("volume", 0) or 0
        cum_pv += tp * vol; cum_vol += vol
        vwap_now = (cum_pv / cum_vol) if cum_vol > 0 else tp
        cum_diffsq += ((tp - vwap_now) ** 2) * vol
    std_now = (cum_diffsq / max(cum_vol, 1.0)) ** 0.5
    return round(vwap_now, 2), round(vwap_now + 1.28 * std_now, 2), round(vwap_now - 1.28 * std_now, 2)


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}")

    results = run_backtest(bars)

    print("\n" + "=" * 70)
    print("BACKTEST RESULTS (real Binance PAXG price history - approximate)")
    print("=" * 70)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        print(f"{name:22s} | Trades: {r['trades']:4d} | Win Rate: {wr:5.1f}% | Total PnL: ${r['pnl']:8.2f} | Avg/Trade: ${avg:6.2f}")

    print("\nNOTE: S3 (Imbalance) and S4 (Tape Momentum) and HFT-Engine could NOT")
    print("be backtested - they need live order-book/tape data not stored historically.")


if __name__ == "__main__":
    main()
