"""
GOLDFLOW BACKTEST v4 - Drawdown analysis + Fixed $3/$6 SL/TP variant
============================================================================
Same reconstructed 1-minute bars + same entry logic as v3 (ATR-normalized
VWAP momentum), but now:
  1. Tracks a running EQUITY CURVE (starting balance $100, 0.01 lot =
     $1 real PnL per $1 price move, confirmed via MT5 XAUUSD.sd contract
     size = 100 oz/lot) and reports MAX DRAWDOWN in $ and %.
  2. Adds a new variant with FIXED $3 SL / $6 TP (not ATR-based) for direct
     comparison against the ATR-normalized variants.

Run: python backtest_engines_v4_drawdown.py
"""

import json
import urllib.request
import urllib.parse
from datetime import datetime
import numpy as np

QUESTDB_URL = "http://127.0.0.1:9010/exec"
STARTING_BALANCE = 100.0


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
    return {**bar, "delta": delta}


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


def max_drawdown(equity_curve):
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0
    return max_dd, max_dd_pct


def run_backtest(bars):
    variants = {
        "V3_RR_1.5 (ATR-based)": {"mode": "atr", "sl_mult": 1.0, "tp_mult": 1.5},
        "V3_RR_2.0 (ATR-based)": {"mode": "atr", "sl_mult": 1.0, "tp_mult": 2.0},
        "Fixed_SL3_TP6":         {"mode": "fixed", "sl": 3.0, "tp": 6.0},
    }
    results = {name: {"trades": 0, "wins": 0, "pnl": 0.0, "equity": [STARTING_BALANCE]} for name in variants}
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
                r = results[name]
                r["trades"] += 1
                r["pnl"] += pnl
                if pnl > 0: r["wins"] += 1
                r["equity"].append(r["equity"][-1] + pnl)
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
                if cfg["mode"] == "atr":
                    sl_dist = cfg["sl_mult"] * atr
                    tp_dist = cfg["tp_mult"] * atr
                else:
                    sl_dist = cfg["sl"]
                    tp_dist = cfg["tp"]
                sl = round(cp - sl_dist, 2) if direction == "BUY" else round(cp + sl_dist, 2)
                tp = round(cp + tp_dist, 2) if direction == "BUY" else round(cp - tp_dist, 2)
                open_pos[name] = {"direction": direction, "entry": cp, "sl": sl, "tp": tp}

    return results


def main():
    ticks = fetch_ticks()
    bars = build_bars(ticks)
    print(f"Reconstructed {len(bars)} 1-minute bars, from {bars[0]['time']} to {bars[-1]['time']}")
    print(f"Assumed: 0.01 lot on XAUUSD.sd (contract size 100oz/lot) => $1 price move = $1 real PnL")
    print(f"Starting balance: ${STARTING_BALANCE:.2f}\n")

    results = run_backtest(bars)

    print("=" * 100)
    print("BACKTEST v4 RESULTS - $100 starting balance, 0.01 lot, with Max Drawdown")
    print("=" * 100)
    for name, r in results.items():
        wr = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
        avg = (r["pnl"] / r["trades"]) if r["trades"] > 0 else 0
        dd, dd_pct = max_drawdown(r["equity"])
        final_balance = r["equity"][-1]
        print(f"{name:24s} | Trades: {r['trades']:4d} | WR: {wr:5.1f}% | Total PnL: ${r['pnl']:8.2f} | "
              f"Avg/Trade: ${avg:5.2f} | Final Balance: ${final_balance:7.2f} | Max Drawdown: ${dd:6.2f} ({dd_pct:.1f}%)")


if __name__ == "__main__":
    main()
